"""L8 orchestration: for one (water body, scene) run the three detectors per zone,
apply the rainfall gate, assign a preliminary severity and persist one
``anomaly_candidates`` row per zone.

Three detectors vote; none decides alone:

* temporal      - zone mean vs its DOY baseline (needs a usable baseline)
* spatial       - DBSCAN plume polygons from the L6 chips (within-scene contrast)
* multivariate  - IsolationForest on the zone's own history

Severity here is provisional (L9 fuses confidence and priority):

    votes 3             -> high
    votes 2             -> high if max |z| >= z_high else medium
    votes 1             -> medium if max |z| >= z_high else low
    votes 0             -> none

then the rainfall gate may cap it at medium and set ``natural_cause_likely``.
A candidate is ``alertable`` only when it has a severity *and* the zone's
temporal baseline is usable; everything else is stored but never alerted on,
with ``suppressed_reason`` saying why.

Idempotent on (water body, scene) through ``anomaly_runs``. Requires the L6 run
to be ``done``; a scene L6 skipped (mask unusable) gets a ``skipped`` run.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from geoalchemy2.shape import from_shape, to_shape
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import (
    AnomalyCandidate,
    AnomalyRun,
    IndicatorObservation,
    IndicatorRun,
    Rainfall,
    Scene,
    WaterBody,
    WaterMaskRecord,
    Zone,
)
from app.services.l05_water_detection.chips import MASK_WATER, read_chip
from app.services.l06_indicators.registry import INDICATORS, QUALITY_INDICATOR_KEYS
from app.services.l06_indicators.zonal import rasterize_zones
from app.services.l07_baseline.rainfall import get_rainfall_context
from app.services.l07_baseline.service import BaselineWindow, get_baseline
from app.services.l08_anomaly.gate import (
    RainfallGateDecision,
    cap_severity,
    doy_percentile,
    rainfall_gate,
)
from app.services.l08_anomaly.multivariate import (
    MultivariateResult,
    SceneFeatures,
    multivariate_detector,
)
from app.services.l08_anomaly.spatial import (
    SpatialCluster,
    SpatialResult,
    spatial_detector,
    union_geom,
)
from app.services.l08_anomaly.temporal import TemporalResult, temporal_detector

log = logging.getLogger(__name__)


class IndicatorsMissingError(LookupError):
    """L6 has not finished for this (water body, scene) yet: retry later."""


class IndicatorsSkippedError(ValueError):
    """L6 skipped the scene (mask unusable); nothing to detect."""


@dataclass(frozen=True)
class ZoneVerdict:
    zone_id: str
    temporal: TemporalResult
    spatial: list[SpatialCluster]
    multivariate: MultivariateResult
    gate: RainfallGateDecision
    votes: int
    severity: str | None
    capped_from: str | None
    natural_cause_likely: bool
    alertable: bool
    suppressed_reason: str | None
    affected_area_km2: float | None


@dataclass
class AnomalyRunResult:
    water_body_id: str
    computed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already done
    unusable: list[str] = field(default_factory=list)  # L6 skipped the scene
    failed: list[str] = field(default_factory=list)


# --- severity -------------------------------------------------------------------


def severity_from_votes(votes: int, max_abs_z: float | None, *, z_high: float) -> str | None:
    strong = max_abs_z is not None and max_abs_z >= z_high
    if votes >= 3:
        return "high"
    if votes == 2:
        return "high" if strong else "medium"
    if votes == 1:
        return "medium" if strong else "low"
    return None


# --- data access ----------------------------------------------------------------


def get_run(session: Session, water_body_id: str, scene_id: str) -> AnomalyRun | None:
    return session.execute(
        select(AnomalyRun).where(
            AnomalyRun.water_body_id == water_body_id, AnomalyRun.scene_id == scene_id
        )
    ).scalar_one_or_none()


def _indicator_run(session: Session, water_body_id: str, scene_id: str) -> IndicatorRun:
    run = session.execute(
        select(IndicatorRun).where(
            IndicatorRun.water_body_id == water_body_id, IndicatorRun.scene_id == scene_id
        )
    ).scalar_one_or_none()
    if run is None or run.status in ("pending", "failed"):
        raise IndicatorsMissingError(f"no finished L6 run for {water_body_id}/{scene_id}")
    if run.status == "skipped":
        raise IndicatorsSkippedError(run.error or "L6 skipped the scene")
    return run


def scene_observations(
    session: Session, water_body_id: str, scene_id: str
) -> dict[str, dict[str, dict[str, float | None]]]:
    """zone_id -> indicator -> {mean, p90, valid_pixel_pct, water_fraction_pct}."""
    rows = session.execute(
        select(
            IndicatorObservation.zone_id,
            IndicatorObservation.indicator,
            IndicatorObservation.mean,
            IndicatorObservation.p90,
            IndicatorObservation.valid_pixel_pct,
            IndicatorObservation.water_fraction_pct,
        ).where(
            IndicatorObservation.water_body_id == water_body_id,
            IndicatorObservation.scene_id == scene_id,
        )
    ).all()
    out: dict[str, dict[str, dict[str, float | None]]] = defaultdict(dict)
    for zone_id, ind, mean, p90, valid, wf in rows:
        out[zone_id][ind] = {
            "mean": mean,
            "p90": p90,
            "valid_pixel_pct": valid,
            "water_fraction_pct": wf,
        }
    return out


def zone_history_features(
    session: Session,
    zone_id: str,
    until: datetime,
    rainfall_by_day: dict[date, float | None],
) -> list[SceneFeatures]:
    """One SceneFeatures per historical scene of the zone (strictly before ``until``)."""
    rows = session.execute(
        select(
            IndicatorObservation.observed_at,
            IndicatorObservation.indicator,
            IndicatorObservation.mean,
            IndicatorObservation.water_fraction_pct,
        )
        .where(
            IndicatorObservation.zone_id == zone_id,
            IndicatorObservation.observed_at < until,
        )
        .order_by(IndicatorObservation.observed_at)
    ).all()
    by_time: dict[datetime, dict[str, Any]] = {}
    for observed_at, ind, mean, wf in rows:
        entry = by_time.setdefault(observed_at, {"indicators": {}, "wf": None})
        entry["indicators"][ind] = mean
        if entry["wf"] is None:
            entry["wf"] = wf
    return [
        SceneFeatures(
            observed_at=t,
            indicators={k: e["indicators"].get(k) for k in QUALITY_INDICATOR_KEYS},
            water_fraction_pct=e["wf"],
            rainfall_72h=rainfall_by_day.get(t.date()),
        )
        for t, e in sorted(by_time.items())
    ]


def rainfall_history(session: Session, water_body_id: str) -> tuple[list[date], list[float]]:
    rows = session.execute(
        select(Rainfall.date, Rainfall.mm_72h)
        .where(Rainfall.water_body_id == water_body_id, Rainfall.mm_72h.is_not(None))
        .order_by(Rainfall.date)
    ).all()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


# --- spatial inputs -------------------------------------------------------------


def _load_spatial_inputs(
    session: Session,
    store: ObjectStore,
    wb: WaterBody,
    scene: Scene,
    l6_run: IndicatorRun,
    zones: list[Zone],
) -> tuple[dict[str, np.ndarray], np.ndarray, dict[str, np.ndarray], Any, str] | None:
    mask_row = session.execute(
        select(WaterMaskRecord).where(
            WaterMaskRecord.water_body_id == wb.id, WaterMaskRecord.scene_id == scene.id
        )
    ).scalar_one_or_none()
    if mask_row is None or not mask_row.chip_key:
        return None
    mask_data, transform, crs = read_chip(store, mask_row.chip_key)
    water = mask_data == MASK_WATER
    rasters: dict[str, np.ndarray] = {}
    for key in QUALITY_INDICATOR_KEYS:
        chip = l6_run.chips.get(key, {}).get("chip_key")
        if not chip:
            continue
        data, _t, _c = read_chip(store, chip)
        if data.shape != water.shape:
            log.warning("chip shape mismatch, skipping spatial", extra={"chip": chip})
            continue
        rasters[key] = data.astype(np.float32)
    if not rasters:
        return None
    zone_rasters = rasterize_zones(
        ((z.id, to_shape(z.geom)) for z in zones), crs, transform, water, water
    )
    zone_masks = {zr.zone_id: zr.mask for zr in zone_rasters}
    return rasters, water, zone_masks, transform, crs


# --- persistence ----------------------------------------------------------------


def upsert_candidate(
    session: Session,
    *,
    wb: WaterBody,
    scene: Scene,
    verdict: ZoneVerdict,
    values: dict[str, dict[str, float | None]],
    rainfall_72h: float | None,
) -> None:
    geom = union_geom(verdict.spatial)
    row = {
        "water_body_id": wb.id,
        "zone_id": verdict.zone_id,
        "scene_id": scene.id,
        "observed_at": scene.sensed_at,
        "indicator_values": values,
        "temporal": verdict.temporal.to_dict(),
        "temporal_flag": verdict.temporal.flagged,
        "max_abs_z": verdict.temporal.max_abs_z,
        "anomalous_indicators": verdict.temporal.anomalous_indicators,
        "spatial": [c.to_dict() for c in verdict.spatial],
        "spatial_flag": bool(verdict.spatial),
        "spatial_geom": None if geom is None else from_shape(geom, srid=4326),
        "affected_area_km2": verdict.affected_area_km2,
        "multivariate_score": verdict.multivariate.score,
        "multivariate_flag": verdict.multivariate.flagged,
        "multivariate": verdict.multivariate.to_dict(),
        "votes": verdict.votes,
        "severity": verdict.severity,
        "natural_cause_likely": verdict.natural_cause_likely,
        "rainfall_gate": {**verdict.gate.to_dict(), "capped_from": verdict.capped_from},
        "rainfall_72h": rainfall_72h,
        "baseline_status": verdict.temporal.baseline_status,
        "alertable": verdict.alertable,
        "suppressed_reason": verdict.suppressed_reason,
        "updated_at": datetime.now(UTC),
    }
    stmt = insert(AnomalyCandidate).values(row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AnomalyCandidate.zone_id, AnomalyCandidate.scene_id],
        set_={k: getattr(stmt.excluded, k) for k in row if k not in ("zone_id", "scene_id")},
    )
    session.execute(stmt)


# --- per-zone verdict -----------------------------------------------------------


def judge_zone(
    zone_id: str,
    values: dict[str, dict[str, float | None]],
    baselines: dict[str, BaselineWindow],
    spatial: list[SpatialCluster],
    history: list[SceneFeatures],
    current: SceneFeatures,
    *,
    rainfall_72h: float | None,
    rainfall_p90: float | None,
    rainfall_n: int,
    settings: Settings,
) -> ZoneVerdict:
    """Pure composition of the three detectors + gate + severity for one zone."""
    temporal = temporal_detector(
        {k: v.get("mean") for k, v in values.items()},
        baselines,
        z_threshold=settings.anomaly_z_threshold,
        sigma_floors=settings.anomaly_sigma_floor,
    )
    multivariate = multivariate_detector(
        history,
        current,
        min_history=settings.multivariate_min_history,
        contamination=settings.multivariate_contamination,
        seed=settings.multivariate_seed,
        window_days=settings.baseline_window_days,
    )
    votes = int(temporal.flagged) + int(bool(spatial)) + int(multivariate.flagged)
    severity = severity_from_votes(votes, temporal.max_abs_z, z_high=settings.anomaly_z_high)

    # The gate reasons over the indicators that deviate: temporal flags first,
    # else the indicators the spatial detector drew a plume for.
    anomalous = temporal.anomalous_indicators or sorted({c.indicator for c in spatial})
    gate = rainfall_gate(
        anomalous,
        rainfall_72h,
        rainfall_p90,
        n_history=rainfall_n,
        min_history=settings.rainfall_gate_min_history,
    )
    severity, capped_from = cap_severity(severity, gate)

    suppressed: str | None = None
    if severity is None:
        suppressed = "no detector voted"
    elif temporal.baseline_status != "usable":
        suppressed = "baseline building: alerts suppressed until the seasonal band is usable"
    alertable = suppressed is None
    area = sum(c.area_km2 for c in spatial) if spatial else None
    return ZoneVerdict(
        zone_id=zone_id,
        temporal=temporal,
        spatial=spatial,
        multivariate=multivariate,
        gate=gate,
        votes=votes,
        severity=severity,
        capped_from=capped_from,
        natural_cause_likely=gate.applied,
        alertable=alertable,
        suppressed_reason=suppressed,
        affected_area_km2=None if area is None else round(area, 4),
    )


# --- orchestration --------------------------------------------------------------


def detect_anomalies(
    session: Session,
    store: ObjectStore,
    wb: WaterBody,
    scene: Scene,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> tuple[AnomalyRun, bool]:
    """Run L8 for one scene over one water body. Returns (run row, did_work)."""
    settings = settings or get_settings()
    run = get_run(session, wb.id, scene.id)
    if run is not None and not force and run.status in ("done", "skipped"):
        return run, False
    if run is None:
        run = AnomalyRun(
            water_body_id=wb.id, scene_id=scene.id, sensed_at=scene.sensed_at, status="pending"
        )
        session.add(run)
        session.flush()

    started = time.perf_counter()
    try:
        try:
            l6_run = _indicator_run(session, wb.id, scene.id)
        except IndicatorsSkippedError as exc:
            run.status = "skipped"
            run.error = str(exc)[:2000]
            run.n_zones = run.n_candidates = run.n_flagged = run.n_alertable = run.n_gated = 0
            run.computed_at = datetime.now(UTC)
            session.flush()
            return run, True

        zones = list(
            session.scalars(
                select(Zone).where(Zone.water_body_id == wb.id).order_by(Zone.seq)
            ).all()
        )
        observations = scene_observations(session, wb.id, scene.id)
        scene_day = scene.sensed_at.date()

        # Rainfall: today's context, the seasonal p90, and a day -> mm_72h map for history.
        ctx = get_rainfall_context(session, wb.id, scene_day)
        rain_days, rain_vals = rainfall_history(session, wb.id)
        rainfall_by_day: dict[date, float | None] = dict(zip(rain_days, rain_vals, strict=True))
        rain_p90, rain_n = doy_percentile(
            rain_days, rain_vals, scene_day, window_days=settings.rainfall_gate_window_days
        )

        # Spatial detector runs once per scene over the whole body.
        spatial_result = SpatialResult()
        spatial_ran = False
        if settings.spatial_enabled:
            inputs = _load_spatial_inputs(session, store, wb, scene, l6_run, zones)
            if inputs is not None:
                rasters, water, zone_masks, transform, crs = inputs
                spatial_result = spatial_detector(
                    rasters,
                    water,
                    zone_masks,
                    transform,
                    crs,
                    sigma_floors=settings.anomaly_sigma_floor,
                    z_threshold=settings.spatial_pixel_z,
                    eps_px=settings.spatial_eps_px,
                    min_samples=settings.spatial_min_samples,
                    min_area_km2=settings.spatial_min_area_km2,
                    max_hot_fraction=settings.spatial_max_hot_fraction,
                    max_hot_pixels=settings.spatial_max_hot_pixels,
                )
                spatial_ran = True

        n_flagged = n_alertable = n_gated = 0
        for zone in zones:
            values = observations.get(zone.id, {})
            baselines = {
                key: get_baseline(session, zone.id, key, scene.sensed_at, settings=settings)
                for key in INDICATORS
            }
            history = zone_history_features(session, zone.id, scene.sensed_at, rainfall_by_day)
            wf = next((v["water_fraction_pct"] for v in values.values()), None)
            current = SceneFeatures(
                observed_at=scene.sensed_at,
                indicators={k: values.get(k, {}).get("mean") for k in QUALITY_INDICATOR_KEYS},
                water_fraction_pct=wf,
                rainfall_72h=ctx.mm_72h,
            )
            verdict = judge_zone(
                zone.id,
                values,
                baselines,
                spatial_result.for_zone(zone.id),
                history,
                current,
                rainfall_72h=ctx.mm_72h,
                rainfall_p90=rain_p90,
                rainfall_n=rain_n,
                settings=settings,
            )
            upsert_candidate(
                session, wb=wb, scene=scene, verdict=verdict, values=values, rainfall_72h=ctx.mm_72h
            )
            n_flagged += verdict.severity is not None
            n_alertable += verdict.alertable
            n_gated += verdict.natural_cause_likely
            if verdict.natural_cause_likely:
                metrics.alerts_gated_rainfall.inc()
            if verdict.severity is not None:
                log.info(
                    "anomaly candidate",
                    extra={
                        "zone_id": zone.id,
                        "scene_id": scene.id,
                        "severity": verdict.severity,
                        "votes": verdict.votes,
                        "anomalous": verdict.temporal.anomalous_indicators,
                        "natural_cause_likely": verdict.natural_cause_likely,
                        "gate": verdict.gate.reason,
                        "alertable": verdict.alertable,
                    },
                )

        run.n_zones = len(zones)
        run.n_candidates = len(zones)
        run.n_flagged = n_flagged
        run.n_alertable = n_alertable
        run.n_gated = n_gated
        run.spatial_ran = spatial_ran
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        session.flush()
        raise

    run.status = "done"
    run.error = None
    run.duration_s = round(time.perf_counter() - started, 3)
    run.computed_at = datetime.now(UTC)
    session.flush()
    log.info(
        "anomalies detected",
        extra={
            "water_body_id": wb.id,
            "scene_id": scene.id,
            "n_zones": run.n_zones,
            "n_flagged": n_flagged,
            "n_alertable": n_alertable,
            "n_gated": n_gated,
            "spatial_skipped": spatial_result.skipped,
            "duration_s": run.duration_s,
        },
    )
    return run, True


def scenes_needing_anomalies(
    session: Session, water_body_id: str, scene_ids: list[str]
) -> list[str]:
    if not scene_ids:
        return []
    done = set(
        session.scalars(
            select(AnomalyRun.scene_id).where(
                AnomalyRun.water_body_id == water_body_id,
                AnomalyRun.scene_id.in_(scene_ids),
                AnomalyRun.status.in_(["done", "skipped"]),
            )
        ).all()
    )
    return [s for s in scene_ids if s not in done]


def indicator_scenes(
    session: Session, water_body_id: str, date_from: date, date_to: date
) -> list[Scene]:
    """Scenes with a finished L6 run for the body, sensed within the window."""
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, 23, 59, 59, tzinfo=UTC)
    stmt = (
        select(Scene)
        .join(IndicatorRun, IndicatorRun.scene_id == Scene.id)
        .where(
            IndicatorRun.water_body_id == water_body_id,
            IndicatorRun.status.in_(["done", "skipped"]),
            Scene.sensed_at >= start,
            Scene.sensed_at <= end,
        )
        .order_by(Scene.sensed_at)
    )
    return list(session.scalars(stmt).all())


def process_water_body(
    session: Session,
    store: ObjectStore,
    water_body_id: str,
    date_from: date,
    date_to: date | None = None,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> AnomalyRunResult:
    """Backfill anomaly candidates for every L6-processed scene in the window,
    oldest first so each scene's multivariate history is what it would have
    been in real time."""
    settings = settings or get_settings()
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    result = AnomalyRunResult(water_body_id=water_body_id)
    for scene in indicator_scenes(session, water_body_id, date_from, date_to or date_from):
        try:
            run, did_work = detect_anomalies(
                session, store, wb, scene, settings=settings, force=force
            )
        except Exception:
            result.failed.append(scene.id)
            log.exception("anomaly detection failed", extra={"scene_id": scene.id})
            raise
        if not did_work:
            result.skipped.append(scene.id)
        elif run.status == "skipped":
            result.unusable.append(scene.id)
        else:
            result.computed.append(scene.id)
    return result
