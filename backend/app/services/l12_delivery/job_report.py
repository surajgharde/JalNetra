"""Pipeline-run report (L12): everything one job produced, day by day, as a PDF
and as a CSV.

Same split as the brief so the renderer is testable without a database:

* ``collect_report_data`` - DB + MinIO (+ Earth Engine) reads -> ``ReportData``
* ``render_report_pdf``   - ``ReportData`` -> PDF bytes
* ``render_report_csv``   - ``ReportData`` -> one row per (day, zone)

PDF layout: a summary page (run header, per-day table, indicator and water-extent
trends, alerts raised in the window, disclaimer), then one page per observed day
with the satellite image, the water mask, the turbidity and chlorophyll rasters,
and the per-zone data table (indicator means, coverage, anomaly z-scores,
severity, priority, rainfall gate). Days with no usable scene still get a row in
the summary so a cloudy week reads as "no observation", not as a gap in the file.

The per-day satellite image is Earth Engine true colour when GEE is enabled
(``report_satellite_source``), otherwise a NIR false-colour composite from the
cached bands - the pipeline never stores blue, so honest true colour needs GEE.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless, before pyplot
import matplotlib.pyplot as plt
import numpy as np
from geoalchemy2.shape import to_shape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import (
    Alert,
    AnomalyCandidate,
    CandidateScore,
    IndicatorObservation,
    IndicatorRun,
    Job,
    Rainfall,
    Scene,
    SceneIngestion,
    WaterBody,
    WaterMaskRecord,
)
from app.services.l03_ingestion.cache import load_bands
from app.services.l05_water_detection.chips import MASK_NODATA, MASK_WATER, chip_key, read_chip
from app.services.l06_indicators.registry import INDICATORS
from app.services.l10_explain.explain import DISCLAIMER

log = logging.getLogger(__name__)

DPI = 110
QUALITY_INDICATORS: tuple[str, ...] = (
    "ndti_turbidity",
    "ndci_chlorophyll",
    "fai_algal",
    "sediment_proxy",
)
INDICATOR_ORDER: tuple[str, ...] = (*QUALITY_INDICATORS, "mndwi_extent")
SHORT_NAME: dict[str, str] = {
    "ndti_turbidity": "NDTI",
    "ndci_chlorophyll": "NDCI",
    "fai_algal": "FAI",
    "sediment_proxy": "Sediment",
    "mndwi_extent": "MNDWI",
}
# Display ramps mirror the tile proxy (app/api/tiles.py) so the PDF matches the map.
RASTER_STYLE: dict[str, tuple[str, float, float]] = {
    "ndti_turbidity": ("YlOrBr", -0.3, 0.5),
    "ndci_chlorophyll": ("Greens", -0.2, 0.4),
    "fai_algal": ("YlGn", -0.05, 0.1),
    "sediment_proxy": ("Oranges", 0.0, 0.3),
    "mndwi_extent": ("Blues", -0.5, 0.8),
}
RASTER_PAGES: tuple[str, ...] = ("ndti_turbidity", "ndci_chlorophyll")  # rasters shown per day
CSV_COLUMNS: tuple[str, ...] = (
    "date",
    "scene_id",
    "platform",
    "source",
    "scene_cloud_pct",
    "mask_usable",
    "valid_pixel_pct",
    "water_extent_km2",
    "rainfall_24h_mm",
    "rainfall_72h_mm",
    "zone_id",
    "zone_name",
    "zone_area_km2",
    *INDICATOR_ORDER,
    *(f"z_{k}" for k in QUALITY_INDICATORS),
    "zone_valid_pixel_pct",
    "zone_water_fraction_pct",
    "max_abs_z",
    "anomalous_indicators",
    "baseline_status",
    "severity",
    "priority_score",
    "confidence",
    "alertable",
    "suppressed_reason",
    "natural_cause_likely",
    "alert_id",
)


# --- data -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RasterImage:
    values: np.ndarray  # float32, NaN where not water / no data
    label: str


@dataclass
class ZoneDay:
    zone_id: str
    zone_name: str
    area_km2: float
    indicators: dict[str, float | None] = field(default_factory=dict)  # mean per indicator
    valid_pixel_pct: float | None = None
    water_fraction_pct: float | None = None
    temporal_z: dict[str, float | None] = field(default_factory=dict)
    max_abs_z: float | None = None
    anomalous: list[str] = field(default_factory=list)
    baseline_status: str | None = None
    severity: str | None = None
    priority_score: float | None = None
    confidence: float | None = None
    alertable: bool | None = None
    suppressed_reason: str | None = None
    natural_cause_likely: bool | None = None
    alert_id: str | None = None


@dataclass
class DayReport:
    observed_on: date
    scene_id: str | None = None
    platform: str | None = None
    source: str | None = None
    scene_cloud_pct: float | None = None
    mask_status: str | None = None  # done | failed | None (never masked)
    mask_usable: bool | None = None
    valid_pixel_pct: float | None = None
    cloud_pixel_pct: float | None = None
    water_extent_km2: float | None = None
    water_fraction_pct: float | None = None
    indicator_status: str | None = None  # done | skipped | failed | None
    rainfall_24h: float | None = None
    rainfall_72h: float | None = None
    zones: list[ZoneDay] = field(default_factory=list)
    satellite_png: bytes | None = None
    satellite_label: str = ""
    watermask: RasterImage | None = None
    rasters: dict[str, RasterImage] = field(default_factory=dict)

    @property
    def observed(self) -> bool:
        return self.scene_id is not None and self.mask_usable is True

    @property
    def n_flagged(self) -> int:
        return sum(1 for z in self.zones if z.anomalous)

    @property
    def n_alertable(self) -> int:
        return sum(1 for z in self.zones if z.alertable)

    @property
    def max_priority(self) -> float | None:
        vals = [z.priority_score for z in self.zones if z.priority_score is not None]
        return max(vals) if vals else None

    def body_mean(self, indicator: str) -> float | None:
        """Area-weighted mean of a zone-level indicator over zones that have it."""
        pairs: list[tuple[float, float]] = [
            (v, z.area_km2) for z in self.zones if (v := z.indicators.get(indicator)) is not None
        ]
        if not pairs:
            return None
        w = sum(a for _, a in pairs)
        return float(sum(v * a for v, a in pairs) / w) if w else None

    def status_text(self) -> str:
        if self.scene_id is None:
            return "no scene"
        if self.mask_status is None:
            return "not processed"
        if self.mask_status != "done":
            return f"mask {self.mask_status}"
        if not self.mask_usable:
            return "too cloudy"
        if self.indicator_status in (None, "skipped"):
            return "masked only"
        if self.indicator_status != "done":
            return f"indicators {self.indicator_status}"
        return "observed"


@dataclass
class ReportData:
    job_id: str
    job_kind: str
    job_status: str
    requested_by: str | None
    water_body_id: str
    water_body_name: str
    district: str
    area_km2: float
    tier: int
    date_from: date
    date_to: date
    days: list[DayReport]  # oldest first; one entry per calendar day with a scene
    alerts: list[dict[str, Any]] = field(default_factory=list)
    scenes_found: int = 0
    scenes_usable: int = 0
    day_pages_limit: int = 60
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def observed_days(self) -> list[DayReport]:
        return [d for d in self.days if d.observed]


@dataclass(frozen=True)
class ReportResult:
    pdf: bytes
    n_pages: int


# --- collection --------------------------------------------------------------------------


def _window(job: Job) -> tuple[datetime, datetime]:
    start = datetime(job.date_from.year, job.date_from.month, job.date_from.day, tzinfo=UTC)
    end = datetime(job.date_to.year, job.date_to.month, job.date_to.day, tzinfo=UTC)
    return start, end + timedelta(days=1)


def _scene_per_day(session: Session, wb: WaterBody, job: Job) -> dict[date, Scene]:
    """The scene to report for each day: prefer one the body was masked on, then usable."""
    start, end = _window(job)
    scenes = session.scalars(
        select(Scene)
        .where(
            Scene.mgrs_tile.in_(list(wb.mgrs_tiles or [])),
            Scene.sensed_at >= start,
            Scene.sensed_at < end,
        )
        .order_by(Scene.sensed_at)
    ).all()
    masked = set(
        session.scalars(
            select(WaterMaskRecord.scene_id).where(
                WaterMaskRecord.water_body_id == wb.id,
                WaterMaskRecord.scene_id.in_([s.id for s in scenes]),
                WaterMaskRecord.status == "done",
            )
        ).all()
    )
    out: dict[date, Scene] = {}
    for s in scenes:
        day = s.sensed_at.date()
        cur = out.get(day)
        rank = (s.id in masked, s.usable, -s.cloud_pct)
        if cur is None or rank > (cur.id in masked, cur.usable, -cur.cloud_pct):
            out[day] = s
    return out


def _raster(store: ObjectStore, key: str, label: str, *, water_only: bool) -> RasterImage | None:
    try:
        data, _t, _crs = read_chip(store, key)
    except Exception as exc:
        log.warning("report: chip unavailable", extra={"key": key, "error": str(exc)})
        return None
    values = np.asarray(data, dtype=np.float32)
    if water_only:
        values = values.copy()
        values[~np.isfinite(values)] = np.nan
    return RasterImage(values=values, label=label)


def _watermask(store: ObjectStore, key: str | None) -> RasterImage | None:
    if not key:
        return None
    try:
        data, _t, _crs = read_chip(store, key)
    except Exception as exc:
        log.warning("report: mask chip unavailable", extra={"key": key, "error": str(exc)})
        return None
    v = np.where(data == MASK_NODATA, np.nan, (data == MASK_WATER).astype(np.float32))
    return RasterImage(values=v.astype(np.float32), label="Water mask")


def false_colour_png(bands: dict[str, np.ndarray], px: int) -> bytes:
    """NIR/red/green composite from the cached bands, 2-98 % stretch per channel."""
    chans = []
    for b in ("B08", "B04", "B03"):
        a = bands[b].astype(np.float32)
        lo, hi = float(np.nanpercentile(a, 2)), float(np.nanpercentile(a, 98))
        chans.append(np.clip((a - lo) / max(hi - lo, 1.0), 0, 1))
    rgb = np.dstack(chans)
    h, w = rgb.shape[:2]
    scale = px / max(h, w)
    fig = plt.figure(figsize=(w * scale / DPI, h * scale / DPI), dpi=DPI)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.imshow(rgb, interpolation="nearest")
    ax.set_axis_off()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return buf.getvalue()


class _Budget:
    """One wall-clock allowance shared by every remote image fetch in a report.

    Each report day wants its own Earth Engine thumbnail, and on a slow link a
    serial run of those outlives any client's timeout -- which also meant the PDF
    never finished, never got stored, and every retry started over. Days past the
    allowance fall back to the locally cached bands instead.

    The allowance has to be enforced here rather than by asking Earth Engine for a
    shorter deadline: ``ee.data.setDeadline`` writes process-global state, so
    lowering it for a report would also shorten every concurrent request. Instead
    each fetch runs on a worker thread and is waited on for no longer than the
    budget has left. A fetch that overruns is abandoned, not cancelled -- the
    thread finishes on its own and its result is dropped -- and the budget is
    emptied so nothing further is submitted.
    """

    def __init__(self, limit_s: float) -> None:
        self.limit_s = limit_s
        self.used_s = 0.0
        self.skipped = 0
        self._pool: ThreadPoolExecutor | None = None

    @property
    def unlimited(self) -> bool:
        return self.limit_s <= 0

    def remaining(self) -> float:
        return math.inf if self.unlimited else max(0.0, self.limit_s - self.used_s)

    def spent(self) -> bool:
        if self.unlimited or self.used_s < self.limit_s:
            return False
        self.skipped += 1
        return True

    def charge(self, seconds: float) -> None:
        self.used_s += seconds

    def run(self, fn: Callable[[], Any]) -> Any:
        """Call ``fn`` on a worker thread, waiting at most the remaining budget.

        Raises :class:`TimeoutError` when the allowance runs out first, having
        emptied the budget so the caller stops submitting.
        """
        if self.unlimited:
            return fn()
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="report-image")
        started = time.perf_counter()
        future = self._pool.submit(fn)
        timed_out = False
        try:
            return future.result(timeout=self.remaining())
        except FuturesTimeout as exc:
            timed_out = True
            raise TimeoutError("image fetch outran the report's budget") from exc
        finally:
            self.charge(time.perf_counter() - started)
            if timed_out:  # abandoned at the limit: charge the wait, then stop submitting
                self.used_s = max(self.used_s, self.limit_s)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None


def _satellite_image(
    store: ObjectStore,
    wb: WaterBody,
    bbox: tuple[float, float, float, float],
    day: date,
    cache_key: str | None,
    settings: Settings,
    budget: _Budget | None = None,
) -> tuple[bytes | None, str]:
    mode = settings.report_satellite_source
    spent = budget is not None and budget.spent()
    if mode in ("auto", "gee") and settings.gee_enabled and not spent:
        from app.services.l03_ingestion import gee

        def fetch() -> bytes | None:
            return gee.truecolor_thumbnail(
                bbox,
                day,
                px=settings.report_image_px,
                settings=settings,
                timeout_s=settings.report_thumbnail_timeout_s,
            )

        try:
            png = budget.run(fetch) if budget is not None else fetch()
            if png:
                return png, "Sentinel-2 true colour (Earth Engine)"
        except Exception as exc:
            log.warning("report: gee thumbnail failed", extra={"day": str(day), "error": str(exc)})
        if mode == "gee":
            return None, "Sentinel-2 true colour unavailable"
    if spent and mode == "gee":
        return None, "Sentinel-2 true colour unavailable (image time budget spent)"
    if cache_key:
        try:
            wbands = load_bands(store, cache_key)
            png = false_colour_png(wbands.arrays, settings.report_image_px)
            return png, "Sentinel-2 false colour NIR/R/G (cached bands)"
        except Exception as exc:
            log.warning("report: cached bands unavailable", extra={"error": str(exc)})
    return None, "satellite image unavailable"


def collect_report_data(
    session: Session,
    store: ObjectStore,
    job: Job,
    *,
    settings: Settings | None = None,
    with_images: bool = True,
) -> ReportData:
    settings = settings or get_settings()
    wb = session.get(WaterBody, job.water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {job.water_body_id!r}")
    zones = sorted(wb.zones, key=lambda z: z.seq)
    geom = to_shape(wb.geom)
    minx, miny, maxx, maxy = geom.buffer(0.001).bounds  # ~100 m, matches the chip margin
    bbox = (float(minx), float(miny), float(maxx), float(maxy))

    per_day = _scene_per_day(session, wb, job)
    scene_ids = [s.id for s in per_day.values()]
    start, end = _window(job)

    masks = {
        r.scene_id: r
        for r in session.scalars(
            select(WaterMaskRecord).where(
                WaterMaskRecord.water_body_id == wb.id, WaterMaskRecord.scene_id.in_(scene_ids)
            )
        ).all()
    }
    runs = {
        r.scene_id: r
        for r in session.scalars(
            select(IndicatorRun).where(
                IndicatorRun.water_body_id == wb.id, IndicatorRun.scene_id.in_(scene_ids)
            )
        ).all()
    }
    ingest_keys: dict[str, str | None] = {
        row[0]: row[1]
        for row in session.execute(
            select(SceneIngestion.scene_id, SceneIngestion.cache_key).where(
                SceneIngestion.water_body_id == wb.id,
                SceneIngestion.scene_id.in_(scene_ids),
                SceneIngestion.status == "done",
            )
        ).all()
    }
    obs: dict[tuple[str, str], dict[str, IndicatorObservation]] = defaultdict(dict)
    for o in session.scalars(
        select(IndicatorObservation).where(
            IndicatorObservation.water_body_id == wb.id,
            IndicatorObservation.scene_id.in_(scene_ids),
        )
    ).all():
        obs[(o.scene_id, o.zone_id)][o.indicator] = o
    cands = {
        (c.scene_id, c.zone_id): c
        for c in session.scalars(
            select(AnomalyCandidate).where(
                AnomalyCandidate.water_body_id == wb.id,
                AnomalyCandidate.scene_id.in_(scene_ids),
            )
        ).all()
    }
    scores = {
        (c.scene_id, c.zone_id): c
        for c in session.scalars(
            select(CandidateScore).where(
                CandidateScore.water_body_id == wb.id, CandidateScore.scene_id.in_(scene_ids)
            )
        ).all()
    }
    rain = {
        r.date: r
        for r in session.scalars(
            select(Rainfall).where(
                Rainfall.water_body_id == wb.id,
                Rainfall.date >= job.date_from,
                Rainfall.date <= job.date_to,
            )
        ).all()
    }
    alerts = session.scalars(
        select(Alert)
        .where(
            Alert.water_body_id == wb.id,
            Alert.first_observed_at >= start,
            Alert.first_observed_at < end,
        )
        .order_by(Alert.priority_score.desc())
    ).all()
    alert_by_zone_day: dict[tuple[str, date], str] = {}
    for a in alerts:
        for entry in a.timeline or []:
            on = entry.get("observed_on")
            if on:
                alert_by_zone_day[(a.zone_id, date.fromisoformat(str(on)))] = a.id
        alert_by_zone_day.setdefault((a.zone_id, a.first_observed_at.date()), a.id)

    days: list[DayReport] = []
    ordered = sorted(per_day.items())
    image_days = {d for d, _ in ordered[-settings.report_max_day_pages :]}
    budget = _Budget(settings.report_image_budget_s)
    for day, scene in ordered:
        mask = masks.get(scene.id)
        run = runs.get(scene.id)
        r = rain.get(day)
        d = DayReport(
            observed_on=day,
            scene_id=scene.id,
            platform=scene.platform,
            source=scene.source,
            scene_cloud_pct=scene.cloud_pct,
            mask_status=mask.status if mask else None,
            mask_usable=mask.usable if mask else None,
            valid_pixel_pct=mask.valid_pixel_pct if mask else None,
            cloud_pixel_pct=mask.cloud_pixel_pct if mask else None,
            water_extent_km2=mask.water_extent_km2 if mask else None,
            water_fraction_pct=mask.water_fraction_pct if mask else None,
            indicator_status=run.status if run else None,
            rainfall_24h=r.mm_24h if r else None,
            rainfall_72h=r.mm_72h if r else None,
        )
        for z in zones:
            zd = ZoneDay(zone_id=z.id, zone_name=z.name, area_km2=z.area_km2)
            zobs = obs.get((scene.id, z.id), {})
            for key in INDICATOR_ORDER:
                ob = zobs.get(key)
                zd.indicators[key] = ob.mean if ob is not None else None
            for first in zobs.values():  # coverage is per zone, identical on every indicator
                zd.valid_pixel_pct = first.valid_pixel_pct
                zd.water_fraction_pct = first.water_fraction_pct
                break
            c = cands.get((scene.id, z.id))
            if c is not None:
                temporal = dict(c.temporal or {})
                for key in QUALITY_INDICATORS:
                    t = temporal.get(key)
                    zd.temporal_z[key] = (
                        t.get("z") if isinstance(t, dict) and t.get("z") is not None else None
                    )
                zd.max_abs_z = c.max_abs_z
                zd.anomalous = list(c.anomalous_indicators or [])
                zd.baseline_status = c.baseline_status
                zd.severity = c.severity
                zd.alertable = c.alertable
                zd.suppressed_reason = c.suppressed_reason
                zd.natural_cause_likely = c.natural_cause_likely
            sc = scores.get((scene.id, z.id))
            if sc is not None:
                zd.priority_score = sc.priority_score
                zd.confidence = sc.confidence
                zd.severity = sc.severity or zd.severity
                zd.alertable = sc.alertable
            zd.alert_id = alert_by_zone_day.get((z.id, day))
            d.zones.append(zd)

        if with_images and d.observed and day in image_days:
            d.satellite_png, d.satellite_label = _satellite_image(
                store, wb, bbox, day, ingest_keys.get(scene.id), settings, budget
            )
            d.watermask = _watermask(store, mask.chip_key if mask else None)
            if run is not None and run.status == "done":
                for key in RASTER_PAGES:
                    ck = (run.chips or {}).get(key, {}).get("chip_key") or chip_key(
                        wb.id, day, key, prefix=settings.chip_prefix
                    )
                    img = _raster(store, ck, INDICATORS[key].display_name, water_only=True)
                    if img is not None:
                        d.rasters[key] = img
        days.append(d)

    budget.close()
    if budget.skipped:
        log.warning(
            "report: satellite image budget spent; remaining days fell back to cached bands",
            extra={
                "job_id": job.id,
                "budget_s": budget.limit_s,
                "used_s": round(budget.used_s, 1),
                "days_skipped": budget.skipped,
            },
        )

    return ReportData(
        job_id=job.id,
        job_kind=job.kind,
        job_status=job.status,
        requested_by=job.requested_by,
        water_body_id=wb.id,
        water_body_name=wb.name,
        district=wb.district,
        area_km2=wb.area_km2,
        tier=wb.tier,
        date_from=job.date_from,
        date_to=job.date_to,
        days=days,
        alerts=[
            {
                "alert_id": a.id,
                "zone": next((z.name for z in zones if z.id == a.zone_id), a.zone_id),
                "indicator": a.primary_indicator,
                "severity": a.severity,
                "priority_score": a.priority_score,
                "confidence": a.confidence,
                "first_observed_on": a.first_observed_at.date().isoformat(),
                "last_observed_on": a.last_observed_at.date().isoformat(),
                "status": a.status,
                "summary": a.summary,
            }
            for a in alerts
        ],
        scenes_found=len(per_day),
        scenes_usable=sum(1 for d in days if d.observed),
        day_pages_limit=settings.report_max_day_pages,
    )


# --- CSV -----------------------------------------------------------------------------------


def render_report_csv(data: ReportData) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for d in data.days:
        base = {
            "date": d.observed_on.isoformat(),
            "scene_id": d.scene_id,
            "platform": d.platform,
            "source": d.source,
            "scene_cloud_pct": d.scene_cloud_pct,
            "mask_usable": d.mask_usable,
            "valid_pixel_pct": d.valid_pixel_pct,
            "water_extent_km2": d.water_extent_km2,
            "rainfall_24h_mm": d.rainfall_24h,
            "rainfall_72h_mm": d.rainfall_72h,
        }
        rows: list[ZoneDay | None] = list(d.zones) or [None]
        for z in rows:
            row: dict[str, Any] = dict(base)
            if z is not None:
                row.update(
                    {
                        "zone_id": z.zone_id,
                        "zone_name": z.zone_name,
                        "zone_area_km2": z.area_km2,
                        **{k: z.indicators.get(k) for k in INDICATOR_ORDER},
                        **{f"z_{k}": z.temporal_z.get(k) for k in QUALITY_INDICATORS},
                        "zone_valid_pixel_pct": z.valid_pixel_pct,
                        "zone_water_fraction_pct": z.water_fraction_pct,
                        "max_abs_z": z.max_abs_z,
                        "anomalous_indicators": ";".join(z.anomalous),
                        "baseline_status": z.baseline_status,
                        "severity": z.severity,
                        "priority_score": z.priority_score,
                        "confidence": z.confidence,
                        "alertable": z.alertable,
                        "suppressed_reason": z.suppressed_reason,
                        "natural_cause_likely": z.natural_cause_likely,
                        "alert_id": z.alert_id,
                    }
                )
            w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


# --- charts -----------------------------------------------------------------------------


def _png(fig: Any) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def trend_chart(data: ReportData) -> io.BytesIO:
    """Body-wide indicator means per observed day (left) and water extent + 72 h rain (right)."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.4, 2.1))
    obs = data.observed_days
    xs = [d.observed_on for d in obs]
    for key in QUALITY_INDICATORS:
        ys = [d.body_mean(key) for d in obs]
        if any(y is not None for y in ys):
            ax1.plot(
                xs,
                [np.nan if y is None else y for y in ys],
                marker="o",
                ms=2.5,
                lw=1,
                label=SHORT_NAME[key],
            )
    ax1.set_title("Body-wide indicator means", fontsize=7)
    ax1.tick_params(labelsize=6)
    ax1.grid(alpha=0.3)
    if obs:
        ax1.legend(fontsize=5.5, ncol=2, frameon=False)
    else:
        ax1.text(0.5, 0.5, "no observed days", ha="center", va="center", fontsize=7)

    ext = [d.water_extent_km2 for d in obs]
    if any(e is not None for e in ext):
        ax2.plot(
            xs,
            [np.nan if e is None else e for e in ext],
            marker="s",
            ms=2.5,
            lw=1,
            color="#1f77b4",
            label="water extent km2",
        )
    ax2.set_title("Water extent and rainfall (72 h)", fontsize=7)
    ax2.tick_params(labelsize=6)
    ax2.grid(alpha=0.3)
    rain_days = [d for d in data.days if d.rainfall_72h is not None]
    if rain_days:
        ax3 = ax2.twinx()
        ax3.bar(
            [d.observed_on for d in rain_days],
            [float(d.rainfall_72h or 0.0) for d in rain_days],
            width=0.8,
            alpha=0.3,
            color="#6baed6",
            label="rain 72 h mm",
        )
        ax3.tick_params(labelsize=6)
    for ax in (ax1, ax2):
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(30)
            lbl.set_ha("right")
    fig.tight_layout()
    return _png(fig)


def raster_png(img: RasterImage, key: str | None) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(3.0, 2.6))
    if key is None:  # water mask
        ax.imshow(img.values, cmap="Blues", vmin=0, vmax=1.4, interpolation="nearest")
    else:
        cmap, vmin, vmax = RASTER_STYLE[key]
        im = ax.imshow(img.values, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
        cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
        cb.ax.tick_params(labelsize=5)
    ax.set_facecolor("#e5e5e5")
    ax.set_xticks([])
    ax.set_yticks([])
    return _png(fig)


# --- PDF -----------------------------------------------------------------------------------


class _PageCounter:
    def __init__(self, job_id: str) -> None:
        self.pages = 0
        self.job_id = job_id

    def __call__(self, canvas: Any, doc: Any) -> None:
        self.pages += 1
        canvas.saveState()
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(
            12 * mm, 6 * mm, f"JalNetra pipeline-run report {self.job_id} - page {self.pages}"
        )
        canvas.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t", parent=base["Title"], fontSize=15, leading=18, spaceAfter=2, alignment=0
        ),
        "sub": ParagraphStyle(
            "s", parent=base["Normal"], fontSize=9, leading=11, textColor=colors.HexColor("#333")
        ),
        "h": ParagraphStyle(
            "h", parent=base["Heading3"], fontSize=9.5, leading=11, spaceBefore=5, spaceAfter=2
        ),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=8, leading=10),
        "small": ParagraphStyle(
            "sm", parent=base["Normal"], fontSize=6.5, leading=8, textColor=colors.HexColor("#444")
        ),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=6.3, leading=7.4),
    }


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int | float):
        return f"{v:.{nd}f}" if isinstance(v, float) else str(v)
    return str(v)


def _table(rows: list[list[Any]], widths: list[float], *, header_bg: str = "#e8eef5") -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 6.3),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 6.3),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#999")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fb")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return t


def _summary_table(data: ReportData) -> Table:
    head = [
        "Date",
        "Scene",
        "Status",
        "Cloud %",
        "Valid %",
        "Water km2",
        *[SHORT_NAME[k] for k in QUALITY_INDICATORS],
        "Flagged",
        "Max prio",
        "Rain 72 h",
    ]
    rows: list[list[Any]] = [head]
    for d in data.days:
        rows.append(
            [
                d.observed_on.isoformat(),
                (d.scene_id or "")[:26],
                d.status_text(),
                _fmt(d.scene_cloud_pct, 0),
                _fmt(d.valid_pixel_pct, 0),
                _fmt(d.water_extent_km2, 2),
                *[_fmt(d.body_mean(k)) for k in QUALITY_INDICATORS],
                f"{d.n_flagged}/{len(d.zones)}" if d.observed else "-",
                _fmt(d.max_priority, 0),
                _fmt(d.rainfall_72h, 1),
            ]
        )
    if len(rows) == 1:
        rows.append(["no scenes in the window"] + [""] * (len(head) - 1))
    widths = [16, 40, 17, 11, 11, 15, 13, 13, 13, 13, 13, 13, 13]
    return _table(rows, [w * mm for w in widths])


def _zone_table(d: DayReport, st: dict[str, ParagraphStyle]) -> Table:
    head = [
        "Zone",
        "km2",
        "Valid %",
        "Water %",
        *[SHORT_NAME[k] for k in INDICATOR_ORDER],
        *[f"z {SHORT_NAME[k][:4]}" for k in QUALITY_INDICATORS],
        "Baseline",
        "Sev.",
        "Prio",
        "Alert",
    ]
    rows: list[list[Any]] = [head]
    for z in d.zones:
        flag = ""
        if z.alertable:
            flag = "alertable"
        elif z.suppressed_reason:
            flag = z.suppressed_reason.replace("_", " ").rstrip(": ")
        if z.alert_id:
            flag = z.alert_id[:14]
        sev = (z.severity or "-") + (" (rain)" if z.natural_cause_likely else "")
        rows.append(
            [
                Paragraph(z.zone_name, st["cell"]),
                _fmt(z.area_km2, 2),
                _fmt(z.valid_pixel_pct, 0),
                _fmt(z.water_fraction_pct, 0),
                *[_fmt(z.indicators.get(k)) for k in INDICATOR_ORDER],
                *[
                    f"<b>{_fmt(z.temporal_z.get(k), 1)}</b>"
                    if k in z.anomalous
                    else _fmt(z.temporal_z.get(k), 1)
                    for k in QUALITY_INDICATORS
                ],
                (z.baseline_status or "-").replace("_", " "),
                sev,
                _fmt(z.priority_score, 0),
                Paragraph(flag, st["cell"]),
            ]
        )
    rows = [
        [Paragraph(c, st["cell"]) if isinstance(c, str) and c.startswith("<b>") else c for c in r]
        for r in rows
    ]
    widths = [22, 8, 9, 9, 11, 11, 11, 11, 11, 9, 9, 9, 9, 13, 10, 8, 16]  # sums to 186 mm
    return _table(rows, [w * mm for w in widths])


def _day_page(d: DayReport, st: dict[str, ParagraphStyle]) -> list[Any]:
    meta = (
        f"scene <b>{d.scene_id}</b> - {d.platform or '?'} via {d.source or '?'} - tile cloud "
        f"{_fmt(d.scene_cloud_pct, 0)} % - valid over body {_fmt(d.valid_pixel_pct, 0)} % - "
        f"water extent <b>{_fmt(d.water_extent_km2, 2)} km2</b> "
        f"({_fmt(d.water_fraction_pct, 0)} % of AOI) - "
        f"rain 24 h {_fmt(d.rainfall_24h, 1)} mm / 72 h {_fmt(d.rainfall_72h, 1)} mm"
    )
    verdict = (
        f"{d.n_flagged} of {len(d.zones)} zones flagged by a detector; {d.n_alertable} alertable"
        + (f"; highest priority {d.max_priority:.0f}/100" if d.max_priority is not None else "")
        + "."
    )
    story: list[Any] = [
        Paragraph(f"{d.observed_on.strftime('%A %d %B %Y')}", st["title"]),
        Paragraph(meta, st["sub"]),
        Paragraph(verdict, st["body"]),
        Spacer(1, 2 * mm),
    ]
    cells: list[Any] = []
    if d.satellite_png:
        cells.append(
            [
                Paragraph(d.satellite_label, st["h"]),
                Image(
                    io.BytesIO(d.satellite_png), width=88 * mm, height=66 * mm, kind="proportional"
                ),
            ]
        )
    else:
        cells.append(
            [
                Paragraph("Satellite image", st["h"]),
                Paragraph(d.satellite_label or "unavailable", st["small"]),
            ]
        )
    if d.watermask is not None:
        cells.append(
            [
                Paragraph("Detected water", st["h"]),
                Image(
                    raster_png(d.watermask, None),
                    width=88 * mm,
                    height=66 * mm,
                    kind="proportional",
                ),
            ]
        )
    else:
        cells.append(
            [Paragraph("Detected water", st["h"]), Paragraph("mask chip unavailable", st["small"])]
        )
    for key in RASTER_PAGES:
        img = d.rasters.get(key)
        name = INDICATORS[key].display_name
        if img is not None:
            cells.append(
                [
                    Paragraph(name, st["h"]),
                    Image(raster_png(img, key), width=88 * mm, height=66 * mm, kind="proportional"),
                ]
            )
        else:
            cells.append(
                [
                    Paragraph(name, st["h"]),
                    Paragraph(
                        "raster unavailable (indicators not computed for this day)", st["small"]
                    ),
                ]
            )
    grid = Table([[cells[0], cells[1]], [cells[2], cells[3]]], colWidths=[92 * mm, 92 * mm])
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story += [grid, Paragraph("Per-zone data", st["h"]), _zone_table(d, st)]
    return story


def render_report_pdf(data: ReportData) -> ReportResult:
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"JalNetra pipeline-run report {data.job_id}",
        author="JalNetra",
    )
    n_obs = len(data.observed_days)
    header = (
        f"<b>{data.water_body_name}</b> ({data.district}, tier {data.tier}, "
        f"{data.area_km2:.2f} km2) - "
        f"window <b>{data.date_from.isoformat()} to {data.date_to.isoformat()}</b> - "
        f"job {data.job_id} "
        f"({data.job_kind}, status <b>{data.job_status}</b>"
        + (f", requested by {data.requested_by}" if data.requested_by else "")
        + ")"
    )
    counts = (
        f"{data.scenes_found} Sentinel-2 pass day(s) found, <b>{n_obs} observed</b> "
        f"(masked and usable), {len(data.alerts)} alert(s) first raised in this window."
    )
    story: list[Any] = [
        Paragraph("JalNetra pipeline-run report", st["title"]),
        Paragraph(header, st["sub"]),
        Paragraph(counts, st["body"]),
        Paragraph("Day by day", st["h"]),
        _summary_table(data),
        Paragraph("Trends over the window", st["h"]),
        Image(trend_chart(data), width=182 * mm, height=52 * mm),
    ]
    if data.alerts:
        story.append(Paragraph("Alerts first raised in this window", st["h"]))
        rows: list[list[Any]] = [
            [
                "Alert",
                "Zone",
                "Indicator",
                "Severity",
                "Priority",
                "Conf.",
                "First",
                "Last",
                "Status",
            ]
        ]
        for a in data.alerts:
            ind = INDICATORS.get(a["indicator"])
            rows.append(
                [
                    a["alert_id"][:18],
                    a["zone"],
                    ind.display_name if ind else a["indicator"],
                    a["severity"],
                    f"{a['priority_score']:.0f}",
                    f"{a['confidence']:.2f}",
                    a["first_observed_on"],
                    a["last_observed_on"],
                    a["status"],
                ]
            )
        story.append(_table(rows, [w * mm for w in (30, 26, 34, 14, 14, 12, 18, 18, 18)]))
        for a in data.alerts[:6]:
            story.append(Paragraph(f"<b>{a['alert_id'][:18]}</b> - {a['summary']}", st["small"]))
    else:
        story.append(Paragraph("Alerts", st["h"]))
        story.append(Paragraph("No alert was raised in this window.", st["body"]))
    if len(data.observed_days) > data.day_pages_limit:
        story.append(
            Paragraph(
                f"Per-day pages are limited to the most recent {data.day_pages_limit} "
                "observed days; the table above and the CSV export cover every day.",
                st["small"],
            )
        )
    story += [
        Spacer(1, 2 * mm),
        Paragraph(
            f"<b>{DISCLAIMER}</b> Indicator values are satellite-derived proxies; z-scores "
            "compare each zone with its own seasonal baseline. "
            f"Generated {data.generated_at:%Y-%m-%d %H:%M} UTC.",
            st["small"],
        ),
    ]
    day_pages = [
        d for d in data.observed_days if d.satellite_png or d.watermask or d.rasters or d.zones
    ]
    for d in day_pages[-data.day_pages_limit :]:
        story.append(PageBreak())
        story += _day_page(d, st)
    counter = _PageCounter(data.job_id)
    doc.build(story, onFirstPage=counter, onLaterPages=counter)
    return ReportResult(pdf=buf.getvalue(), n_pages=counter.pages)


# --- orchestration ------------------------------------------------------------------------


def report_key(job: Job, fmt: str, settings: Settings) -> str:
    return f"{settings.report_prefix}/{job.id}.{fmt}"


def build_report(
    session: Session,
    store: ObjectStore,
    job: Job,
    fmt: str,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> bytes:
    """PDF or CSV bytes for a job. A finished job's report is immutable, so it is
    stored once in MinIO and served from there; a running job is rendered live."""
    settings = settings or get_settings()
    if fmt not in ("pdf", "csv"):
        raise ValueError(f"unknown report format {fmt!r}")
    key = report_key(job, fmt, settings)
    if job.status == "done" and not refresh and store.exists(key):
        return store.get_bytes(key)
    data = collect_report_data(session, store, job, settings=settings, with_images=fmt == "pdf")
    if fmt == "pdf":
        result = render_report_pdf(data)
        blob = result.pdf
        log.info(
            "report rendered", extra={"job_id": job.id, "pages": result.n_pages, "bytes": len(blob)}
        )
    else:
        blob = render_report_csv(data).encode("utf-8")
    if job.status == "done":
        store.put_bytes(key, blob, content_type="application/pdf" if fmt == "pdf" else "text/csv")
    return blob
