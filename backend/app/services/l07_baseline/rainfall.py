"""Rainfall covariate (S5, L7): Open-Meteo daily precipitation at each water body
centroid, stored as mm_24h / mm_72h in the ``rainfall`` table.

Two Open-Meteo endpoints are used because neither covers the whole timeline:

* the **archive** API (ERA5 reanalysis, 1940 -> ~5 days ago) for history and the
  nightly refresh;
* the **forecast** API with ``past_days`` (model analysis, last 92 days) to fill
  the archive's lag, so a scene processed today still gets a rainfall context.

Archive rows overwrite forecast rows once the archive catches up; a forecast row
never overwrites an archive row. Both are free, keyless, and rate-limited to
~10k requests/day, which is far above 500 bodies x 1 call/day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Rainfall, WaterBody

log = logging.getLogger(__name__)

SOURCE_ARCHIVE = "open-meteo-archive"
SOURCE_FORECAST = "open-meteo-forecast"
FORECAST_MAX_PAST_DAYS = 92
ARCHIVE_MAX_SPAN_DAYS = 366 * 5  # keep one request's daily array a manageable size


class RainfallSourceError(RuntimeError):
    """Open-Meteo unreachable or returned malformed data (retryable)."""


@dataclass(frozen=True)
class DailyPrecip:
    day: date
    mm: float
    source: str


@dataclass(frozen=True)
class RainfallContext:
    """What L8/L9 read for a scene date. ``available`` is False when the table has
    no row for that day, in which case the covariate must not be applied."""

    water_body_id: str
    date: date
    mm_24h: float | None
    mm_72h: float | None
    mm_7d: float | None
    source: str | None
    available: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "water_body_id": self.water_body_id,
            "date": self.date.isoformat(),
            "mm_24h": self.mm_24h,
            "mm_72h": self.mm_72h,
            "mm_7d": self.mm_7d,
            "source": self.source,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass
class RainfallSyncResult:
    water_body_id: str
    date_from: date
    date_to: date
    archive_rows: int = 0
    forecast_rows: int = 0
    errors: list[str] = field(default_factory=list)


# --- HTTP -----------------------------------------------------------------------


def _parse_daily(payload: dict[str, Any], source: str) -> list[DailyPrecip]:
    try:
        days = payload["daily"]["time"]
        mm = payload["daily"]["precipitation_sum"]
    except (KeyError, TypeError) as exc:
        raise RainfallSourceError(f"unexpected Open-Meteo payload: {exc}") from exc
    out: list[DailyPrecip] = []
    for d, v in zip(days, mm, strict=True):
        if v is None:  # archive returns null for days it has not assimilated yet
            continue
        out.append(DailyPrecip(date.fromisoformat(d), float(v), source))
    return out


def _get(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        r = httpx.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise RainfallSourceError(f"Open-Meteo request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise RainfallSourceError("Open-Meteo returned a non-object body")
    return data


def fetch_archive(
    lat: float, lon: float, start: date, end: date, *, settings: Settings | None = None
) -> list[DailyPrecip]:
    """Daily precipitation from the ERA5 archive for [start, end], in UTC days."""
    settings = settings or get_settings()
    out: list[DailyPrecip] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=ARCHIVE_MAX_SPAN_DAYS - 1))
        data = _get(
            settings.open_meteo_archive_url,
            {
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "daily": "precipitation_sum",
                "timezone": "UTC",
            },
            settings.open_meteo_timeout_s,
        )
        out.extend(_parse_daily(data, SOURCE_ARCHIVE))
        chunk_start = chunk_end + timedelta(days=1)
    return out


def fetch_recent(
    lat: float, lon: float, past_days: int, *, settings: Settings | None = None
) -> list[DailyPrecip]:
    """Daily precipitation for the last ``past_days`` days (plus today) from the
    forecast API's analysis. Today's value is partial and is re-fetched tomorrow."""
    settings = settings or get_settings()
    data = _get(
        settings.open_meteo_forecast_url,
        {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "past_days": min(max(past_days, 1), FORECAST_MAX_PAST_DAYS),
            "forecast_days": 1,
            "daily": "precipitation_sum",
            "timezone": "UTC",
        },
        settings.open_meteo_timeout_s,
    )
    return _parse_daily(data, SOURCE_FORECAST)


# --- persistence ----------------------------------------------------------------


def water_body_centroid(session: Session, water_body_id: str) -> tuple[float, float]:
    """(lat, lon) of the body's centroid; point-on-surface so a crescent-shaped
    reservoir does not put the point on land."""
    row = session.execute(
        select(
            func.ST_Y(func.ST_PointOnSurface(WaterBody.geom)),
            func.ST_X(func.ST_PointOnSurface(WaterBody.geom)),
        ).where(WaterBody.id == water_body_id)
    ).one_or_none()
    if row is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    return float(row[0]), float(row[1])


def rolling_72h(days: list[DailyPrecip], prior: dict[date, float]) -> dict[date, float | None]:
    """Trailing 3-day sum per day; None when any of the two prior days is unknown.
    ``prior`` supplies days already in the table just before the fetched range."""
    known: dict[date, float] = dict(prior)
    known.update({d.day: d.mm for d in days})
    out: dict[date, float | None] = {}
    for d in days:
        parts = [known.get(d.day - timedelta(days=k)) for k in range(3)]
        out[d.day] = (
            None if any(p is None for p in parts) else float(sum(p for p in parts if p is not None))
        )
    return out


def upsert_rainfall(session: Session, water_body_id: str, days: list[DailyPrecip]) -> int:
    """Write daily rows. Archive rows overwrite anything; forecast rows only fill
    gaps or refresh other forecast rows."""
    if not days:
        return 0
    first = min(d.day for d in days)
    prior_rows = session.execute(
        select(Rainfall.date, Rainfall.mm_24h).where(
            Rainfall.water_body_id == water_body_id,
            Rainfall.date >= first - timedelta(days=2),
            Rainfall.date < first,
        )
    ).all()
    prior = {r[0]: float(r[1]) for r in prior_rows}
    mm72 = rolling_72h(days, prior)
    now = datetime.now(UTC)
    written = 0
    for source in (SOURCE_ARCHIVE, SOURCE_FORECAST):
        batch = [
            {
                "water_body_id": water_body_id,
                "date": d.day,
                "mm_24h": d.mm,
                "mm_72h": mm72[d.day],
                "source": d.source,
                "fetched_at": now,
            }
            for d in days
            if d.source == source
        ]
        if not batch:
            continue
        stmt = insert(Rainfall).values(batch)
        set_ = {
            "mm_24h": stmt.excluded.mm_24h,
            "mm_72h": stmt.excluded.mm_72h,
            "source": stmt.excluded.source,
            "fetched_at": stmt.excluded.fetched_at,
        }
        if source == SOURCE_ARCHIVE:
            stmt = stmt.on_conflict_do_update(
                index_elements=[Rainfall.water_body_id, Rainfall.date], set_=set_
            )
        else:
            stmt = stmt.on_conflict_do_update(
                index_elements=[Rainfall.water_body_id, Rainfall.date],
                set_=set_,
                where=Rainfall.source != SOURCE_ARCHIVE,
            )
        res = session.execute(stmt)
        written += int(getattr(res, "rowcount", 0) or 0)
    return written


def sync_rainfall(
    session: Session,
    water_body_id: str,
    date_from: date,
    date_to: date | None = None,
    *,
    settings: Settings | None = None,
    today: date | None = None,
) -> RainfallSyncResult:
    """Fetch and store [date_from, date_to] for one body: archive for the part the
    archive covers, forecast analysis for the trailing lag window."""
    settings = settings or get_settings()
    today = today or datetime.now(UTC).date()
    date_to = min(date_to or today, today)
    result = RainfallSyncResult(water_body_id, date_from, date_to)
    lat, lon = water_body_centroid(session, water_body_id)

    archive_end = min(date_to, today - timedelta(days=settings.rainfall_archive_lag_days))
    if archive_end >= date_from:
        # Fetch two extra days at the front so the first mm_72h is complete even
        # when the table has nothing before date_from.
        days = fetch_archive(
            lat, lon, date_from - timedelta(days=2), archive_end, settings=settings
        )
        result.archive_rows = upsert_rainfall(session, water_body_id, days)

    if date_to > archive_end:
        past = (today - max(date_from, archive_end + timedelta(days=1))).days + 2
        recent = fetch_recent(lat, lon, past, settings=settings)
        recent = [d for d in recent if archive_end < d.day <= date_to]
        result.forecast_rows = upsert_rainfall(session, water_body_id, recent)
    return result


def sync_rainfall_recent(
    session: Session, water_body_id: str, *, settings: Settings | None = None
) -> RainfallSyncResult:
    """The daily job: refresh the lookback window so forecast rows get replaced
    by archive rows as ERA5 catches up."""
    settings = settings or get_settings()
    today = datetime.now(UTC).date()
    return sync_rainfall(
        session,
        water_body_id,
        today - timedelta(days=settings.rainfall_lookback_days),
        today,
        settings=settings,
        today=today,
    )


def backfill_rainfall(
    session: Session,
    water_body_id: str,
    *,
    date_from: date | None = None,
    settings: Settings | None = None,
) -> RainfallSyncResult:
    """Bulk history pull, from ``rainfall_history_start`` (or the day after the
    last archive row) to today."""
    settings = settings or get_settings()
    if date_from is None:
        last = session.execute(
            select(func.max(Rainfall.date)).where(
                Rainfall.water_body_id == water_body_id, Rainfall.source == SOURCE_ARCHIVE
            )
        ).scalar_one_or_none()
        date_from = (
            last + timedelta(days=1)
            if last
            else date.fromisoformat(settings.rainfall_history_start)
        )
    return sync_rainfall(session, water_body_id, date_from, settings=settings)


# --- read side ------------------------------------------------------------------


def get_rainfall_context(
    session: Session, water_body_id: str, when: datetime | date
) -> RainfallContext:
    """Rainfall on and before a scene date. mm_7d is summed from stored rows and
    is None when any of the seven days is missing."""
    day = when.date() if isinstance(when, datetime) else when
    rows = session.execute(
        select(Rainfall.date, Rainfall.mm_24h, Rainfall.mm_72h, Rainfall.source)
        .where(
            Rainfall.water_body_id == water_body_id,
            Rainfall.date > day - timedelta(days=7),
            Rainfall.date <= day,
        )
        .order_by(Rainfall.date)
    ).all()
    by_day = {r[0]: r for r in rows}
    today_row = by_day.get(day)
    if today_row is None:
        return RainfallContext(
            water_body_id, day, None, None, None, None, False, "no rainfall row for this day"
        )
    mm_7d: float | None = None
    if len(by_day) == 7:
        mm_7d = float(sum(r[1] for r in rows))
    return RainfallContext(
        water_body_id=water_body_id,
        date=day,
        mm_24h=float(today_row[1]),
        mm_72h=None if today_row[2] is None else float(today_row[2]),
        mm_7d=mm_7d,
        source=str(today_row[3]),
        available=True,
    )


def rainfall_series(
    session: Session, water_body_id: str, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Rainfall.date, Rainfall.mm_24h, Rainfall.mm_72h, Rainfall.source)
        .where(
            Rainfall.water_body_id == water_body_id,
            Rainfall.date >= date_from,
            Rainfall.date <= date_to,
        )
        .order_by(Rainfall.date)
    ).all()
    return [
        {"date": r[0].isoformat(), "mm_24h": r[1], "mm_72h": r[2], "source": r[3]} for r in rows
    ]
