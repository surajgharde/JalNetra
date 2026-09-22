"""L7 baseline orchestration: hypertable history -> ``baselines`` rows, plus the
read side (``get_baseline``) that L8 and the API consume.

A baseline is rebuilt wholesale per (zone, indicator): the full year of DOY
windows is recomputed from every observation in the hypertable and upserted.
That is a few hundred rows per series and takes milliseconds, so there is no
incremental path to get wrong.

Alert suppression contract: ``get_baseline`` returns ``status="building"``
whenever the window has fewer than ``baseline_min_samples`` observations or the
zone's history is shorter than the tier's minimum span. L8 must not raise an
alert against a building baseline, and the API shows the zone as "baseline
building" instead of a band.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import Connection, Integer, case, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Baseline, BaselineSample, IndicatorObservation, WaterBody, Zone
from app.services.l06_indicators.registry import INDICATORS
from app.services.l07_baseline.robust import (
    WindowStats,
    build_seasonal_baseline,
    day_of_year,
)

log = logging.getLogger(__name__)

STATUS_USABLE = "usable"
STATUS_BUILDING = "building"

_BASELINE_UPSERT_COLS = (
    "water_body_id",
    "window_days",
    "mean",
    "std",
    "p10",
    "p90",
    "n_samples",
    "n_years",
    "status",
    "history_from",
    "history_to",
    "history_days",
    "computed_at",
)


@dataclass(frozen=True)
class BaselineWindow:
    """What L8 gets back for one (zone, indicator, date)."""

    zone_id: str
    indicator: str
    doy_window: int
    window_days: int
    mean: float | None  # median of the historical window
    std: float | None  # 1.4826 * MAD
    p10: float | None
    p90: float | None
    n_samples: int
    n_years: int
    history_days: int
    status: str  # usable | building
    reason: str | None = None  # why it is building, for the UI / brief

    @property
    def usable(self) -> bool:
        return self.status == STATUS_USABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "indicator": self.indicator,
            "doy_window": self.doy_window,
            "window_days": self.window_days,
            "mean": self.mean,
            "std": self.std,
            "p10": self.p10,
            "p90": self.p90,
            "n_samples": self.n_samples,
            "n_years": self.n_years,
            "history_days": self.history_days,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class BaselineBuildResult:
    water_body_id: str
    series_built: int = 0
    rows_written: int = 0
    usable_windows: int = 0
    building_windows: int = 0
    zones: list[str] = field(default_factory=list)
    history_from: datetime | None = None
    history_to: datetime | None = None


@dataclass(frozen=True)
class SeriesHistory:
    observed_at: list[datetime]
    values: list[float]

    @property
    def span_days(self) -> int:
        if not self.observed_at:
            return 0
        return (max(self.observed_at) - min(self.observed_at)).days

    @property
    def first(self) -> datetime | None:
        return min(self.observed_at) if self.observed_at else None

    @property
    def last(self) -> datetime | None:
        return max(self.observed_at) if self.observed_at else None


# --- history --------------------------------------------------------------------


def load_history(
    session: Session,
    zone_id: str,
    indicator: str,
    *,
    until: datetime | None = None,
    statistic: str = "mean",
) -> SeriesHistory:
    """Every accepted observation of one series, oldest first. ``until`` lets a
    rebuild exclude the scene being scored (leave-one-out for validation)."""
    col = getattr(IndicatorObservation, statistic)
    stmt = (
        select(IndicatorObservation.observed_at, col)
        .where(
            IndicatorObservation.zone_id == zone_id,
            IndicatorObservation.indicator == indicator,
            col.is_not(None),
        )
        .order_by(IndicatorObservation.observed_at)
    )
    if until is not None:
        stmt = stmt.where(IndicatorObservation.observed_at < until)
    rows = [(r[0], float(r[1])) for r in session.execute(stmt).all()]
    if statistic == "mean":
        # Field-validated true negatives (S11) count as extra confirmed-normal samples.
        extra = select(BaselineSample.observed_at, BaselineSample.value).where(
            BaselineSample.zone_id == zone_id, BaselineSample.indicator == indicator
        )
        if until is not None:
            extra = extra.where(BaselineSample.observed_at < until)
        rows += [(r[0], float(r[1])) for r in session.execute(extra).all()]
        rows.sort(key=lambda r: r[0])
    return SeriesHistory([r[0] for r in rows], [r[1] for r in rows])


def min_history_days(tier: int, settings: Settings) -> int:
    return (
        settings.baseline_min_history_days_tier1
        if tier == 1
        else settings.baseline_min_history_days
    )


def window_status(
    stats: WindowStats, history_days: int, *, tier: int, settings: Settings
) -> tuple[str, str | None]:
    if stats.n_samples < settings.baseline_min_samples:
        return STATUS_BUILDING, (
            f"{stats.n_samples} historical observations in the window; "
            f"need {settings.baseline_min_samples}"
        )
    need = min_history_days(tier, settings)
    if history_days < need:
        return STATUS_BUILDING, f"{history_days} days of history; Tier {tier} needs {need}"
    return STATUS_USABLE, None


def _tier_of_zone(session: Session, zone_id: str) -> int:
    tier = session.execute(
        select(WaterBody.tier)
        .join(Zone, Zone.water_body_id == WaterBody.id)
        .where(Zone.id == zone_id)
    ).scalar_one_or_none()
    return int(tier) if tier is not None else 3


# --- build ----------------------------------------------------------------------


def build_series_baseline(
    session: Session,
    zone: Zone,
    indicator: str,
    *,
    tier: int,
    settings: Settings | None = None,
    until: datetime | None = None,
) -> tuple[int, int, SeriesHistory]:
    """Recompute and upsert the full year of windows for one (zone, indicator).
    Returns (usable_windows, building_windows, history)."""
    settings = settings or get_settings()
    history = load_history(session, zone.id, indicator, until=until)
    windows = build_seasonal_baseline(
        history.observed_at, history.values, window_days=settings.baseline_window_days
    )
    now = datetime.now(UTC)
    usable = building = 0
    rows: list[dict[str, Any]] = []
    for w in windows:
        status, _ = window_status(w, history.span_days, tier=tier, settings=settings)
        usable += status == STATUS_USABLE
        building += status == STATUS_BUILDING
        rows.append(
            {
                "zone_id": zone.id,
                "indicator": indicator,
                "doy_window": w.doy,
                "water_body_id": zone.water_body_id,
                "window_days": settings.baseline_window_days,
                "mean": w.median,
                "std": w.sigma,
                "p10": w.p10,
                "p90": w.p90,
                "n_samples": w.n_samples,
                "n_years": w.n_years,
                "status": status,
                "history_from": history.first,
                "history_to": history.last,
                "history_days": history.span_days,
                "computed_at": now,
            }
        )
    stmt = insert(Baseline).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Baseline.zone_id, Baseline.indicator, Baseline.doy_window],
        set_={c: getattr(stmt.excluded, c) for c in _BASELINE_UPSERT_COLS},
    )
    session.execute(stmt)
    return usable, building, history


def build_water_body_baselines(
    session: Session,
    water_body_id: str,
    *,
    indicators: list[str] | None = None,
    settings: Settings | None = None,
) -> BaselineBuildResult:
    """Rebuild every (zone, indicator) baseline of a water body."""
    settings = settings or get_settings()
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    keys = indicators or list(INDICATORS)
    unknown = set(keys) - set(INDICATORS)
    if unknown:
        raise KeyError(f"unknown indicators: {sorted(unknown)}")
    result = BaselineBuildResult(water_body_id=water_body_id)
    for zone in wb.zones:
        result.zones.append(zone.id)
        for key in keys:
            usable, building, history = build_series_baseline(
                session, zone, key, tier=wb.tier, settings=settings
            )
            result.series_built += 1
            result.rows_written += usable + building
            result.usable_windows += usable
            result.building_windows += building
            if history.first is not None:
                result.history_from = (
                    history.first
                    if result.history_from is None
                    else min(result.history_from, history.first)
                )
            if history.last is not None:
                result.history_to = (
                    history.last
                    if result.history_to is None
                    else max(result.history_to, history.last)
                )
    log.info(
        "baselines built",
        extra={
            "water_body_id": water_body_id,
            "series": result.series_built,
            "usable": result.usable_windows,
            "building": result.building_windows,
        },
    )
    return result


def water_bodies_with_observations(session: Session, *, tier: int | None = None) -> list[str]:
    """Bodies that have at least one accepted observation (so a rebuild is meaningful)."""
    stmt = select(IndicatorObservation.water_body_id).distinct()
    if tier is not None:
        stmt = stmt.join(WaterBody, WaterBody.id == IndicatorObservation.water_body_id).where(
            WaterBody.tier == tier
        )
    return sorted(str(x) for x in session.scalars(stmt).all())


def drop_baselines(session: Session, water_body_id: str) -> int:
    res = session.execute(delete(Baseline).where(Baseline.water_body_id == water_body_id))
    return int(getattr(res, "rowcount", 0) or 0)


# --- read side ------------------------------------------------------------------


def _row_to_window(row: Baseline, *, tier: int, settings: Settings) -> BaselineWindow:
    stats = WindowStats(
        row.doy_window, row.mean, row.std, row.p10, row.p90, row.n_samples, row.n_years
    )
    # Recomputed from the row rather than trusting the persisted status, so a
    # raised threshold takes effect without a rebuild.
    status, reason = window_status(stats, row.history_days, tier=tier, settings=settings)
    return BaselineWindow(
        zone_id=row.zone_id,
        indicator=row.indicator,
        doy_window=row.doy_window,
        window_days=row.window_days,
        mean=row.mean,
        std=row.std,
        p10=row.p10,
        p90=row.p90,
        n_samples=row.n_samples,
        n_years=row.n_years,
        history_days=row.history_days,
        status=status,
        reason=reason,
    )


def _empty_window(
    zone_id: str, indicator: str, doy: int, settings: Settings, reason: str
) -> BaselineWindow:
    return BaselineWindow(
        zone_id=zone_id,
        indicator=indicator,
        doy_window=doy,
        window_days=settings.baseline_window_days,
        mean=None,
        std=None,
        p10=None,
        p90=None,
        n_samples=0,
        n_years=0,
        history_days=0,
        status=STATUS_BUILDING,
        reason=reason,
    )


def get_baseline(
    session: Session,
    zone_id: str,
    indicator: str,
    when: datetime | date,
    *,
    settings: Settings | None = None,
) -> BaselineWindow:
    """DOY-matched window for a zone-indicator on a date. Never raises for a
    missing baseline: returns a ``building`` window with n_samples=0 so callers
    have one code path."""
    settings = settings or get_settings()
    doy = day_of_year(when)
    row = session.get(Baseline, (zone_id, indicator, doy))
    if row is None:
        return _empty_window(zone_id, indicator, doy, settings, "no baseline built yet")
    return _row_to_window(row, tier=_tier_of_zone(session, zone_id), settings=settings)


def get_baseline_year(
    session: Session, zone_id: str, indicator: str, *, settings: Settings | None = None
) -> list[BaselineWindow]:
    """All 366 windows of a series, for drawing the seasonal band on the chart."""
    settings = settings or get_settings()
    tier = _tier_of_zone(session, zone_id)
    rows = session.scalars(
        select(Baseline)
        .where(Baseline.zone_id == zone_id, Baseline.indicator == indicator)
        .order_by(Baseline.doy_window)
    ).all()
    return [_row_to_window(r, tier=tier, settings=settings) for r in rows]


def zone_baseline_status(session: Session, zone_id: str) -> dict[str, Any]:
    """Per-indicator summary for the zone card: usable window share and history span."""
    usable_flag = case((Baseline.status == STATUS_USABLE, 1), else_=0)
    stmt = (
        select(
            Baseline.indicator,
            func.count().label("n_windows"),
            func.sum(func.cast(usable_flag, Integer)).label("usable"),
            func.max(Baseline.history_days),
            func.max(Baseline.n_years),
            func.min(Baseline.history_from),
            func.max(Baseline.history_to),
            func.max(Baseline.computed_at),
        )
        .where(Baseline.zone_id == zone_id)
        .group_by(Baseline.indicator)
    )
    out: dict[str, Any] = {}
    for ind, n_windows, usable, hist_days, n_years, h_from, h_to, computed in session.execute(stmt):
        usable = int(usable or 0)
        out[str(ind)] = {
            "status": STATUS_USABLE if usable == n_windows else STATUS_BUILDING,
            "usable_windows": usable,
            "n_windows": int(n_windows),
            "history_days": int(hist_days or 0),
            "n_years": int(n_years or 0),
            "history_from": h_from.isoformat() if h_from else None,
            "history_to": h_to.isoformat() if h_to else None,
            "computed_at": computed.isoformat() if computed else None,
        }
    for key in INDICATORS:
        out.setdefault(
            key,
            {
                "status": STATUS_BUILDING,
                "usable_windows": 0,
                "n_windows": 0,
                "history_days": 0,
                "n_years": 0,
                "history_from": None,
                "history_to": None,
                "computed_at": None,
            },
        )
    return out


# --- weekly continuous aggregate ------------------------------------------------


def refresh_weekly(
    session: Session, start: datetime | None = None, end: datetime | None = None
) -> None:
    """Refresh ``indicator_weekly`` over a range (default: the whole archive).

    ``CALL refresh_continuous_aggregate`` cannot run inside a transaction, so
    this commits the session and runs on an autocommit connection. Called after
    a bulk backfill; the hourly policy covers the live window on its own.
    """
    session.commit()
    # A fresh connection: the session's own would auto-begin a transaction first,
    # and the isolation level cannot be changed once one is open.
    bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text(
                "CALL refresh_continuous_aggregate('indicator_weekly', "
                "CAST(:start AS timestamptz), CAST(:end AS timestamptz))"
            ),
            {"start": start, "end": end},
        )


def weekly_series(
    session: Session,
    zone_id: str,
    indicator: str,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    """Weekly means from the continuous aggregate, oldest first."""
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
    rows = session.execute(
        text(
            """
            SELECT week, mean, p90, p90_max, mean_min, mean_max, valid_pixel_pct, n_obs
            FROM indicator_weekly
            WHERE zone_id = :zone_id AND indicator = :indicator
              AND week >= :start AND week < :end
            ORDER BY week
            """
        ),
        {"zone_id": zone_id, "indicator": indicator, "start": start, "end": end},
    ).all()
    return [
        {
            "week": r[0].isoformat(),
            "mean": r[1],
            "p90": r[2],
            "p90_max": r[3],
            "mean_min": r[4],
            "mean_max": r[5],
            "valid_pixel_pct": r[6],
            "n_obs": int(r[7]),
        }
        for r in rows
    ]


def series_with_band(
    session: Session,
    zone_id: str,
    indicator: str,
    date_from: date,
    date_to: date,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Raw observations plus the DOY-matched baseline band for each point: the
    payload the S10 chart draws (value line inside the seasonal p10-p90 band)."""
    settings = settings or get_settings()
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
    obs = session.execute(
        select(
            IndicatorObservation.observed_at,
            IndicatorObservation.mean,
            IndicatorObservation.p90,
            IndicatorObservation.scene_id,
            IndicatorObservation.valid_pixel_pct,
        )
        .where(
            IndicatorObservation.zone_id == zone_id,
            IndicatorObservation.indicator == indicator,
            IndicatorObservation.observed_at >= start,
            IndicatorObservation.observed_at < end,
        )
        .order_by(IndicatorObservation.observed_at)
    ).all()
    year = {
        w.doy_window: w for w in get_baseline_year(session, zone_id, indicator, settings=settings)
    }
    points = []
    for observed_at, mean, p90, scene_id, valid in obs:
        w = year.get(day_of_year(observed_at))
        points.append(
            {
                "observed_at": observed_at.isoformat(),
                "scene_id": scene_id,
                "mean": mean,
                "p90": p90,
                "valid_pixel_pct": valid,
                "baseline": None if w is None else w.to_dict(),
            }
        )
    usable = sum(1 for w in year.values() if w.usable)
    return {
        "zone_id": zone_id,
        "indicator": indicator,
        "points": points,
        "baseline_status": STATUS_USABLE if year and usable == len(year) else STATUS_BUILDING,
        "baseline_usable_windows": usable,
        "baseline_windows": len(year),
    }


def outlier_sensitivity(values: list[float], spike: float) -> float:
    """Relative shift of the robust centre when one ``spike`` is appended: the
    plan's acceptance check (<= 5 %), exposed for the validation CLI."""
    base = float(np.median(np.asarray(values, dtype=np.float64)))
    with_spike = float(np.median(np.asarray([*values, spike], dtype=np.float64)))
    return abs(with_spike - base) / max(abs(base), 1e-9)
