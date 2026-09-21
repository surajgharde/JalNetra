"""L6 orchestration: cached bands + water-mask chip -> indicator rasters -> zonal
records in the hypertable + one COG chip per indicator.

Idempotent on (water_body_id, scene_id) through ``indicator_runs``. Requires the
S3 mask to exist and be usable; a rejected scene gets a ``skipped`` run row and
no observations, so a gap in a zone's series always means "not observable".

The water mask is *read back from its chip* rather than recomputed, so the pixels
an indicator describes are exactly the pixels the map shows as water.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import (
    IndicatorObservation,
    IndicatorRun,
    RasterChip,
    Scene,
    SceneIngestion,
    WaterBody,
    WaterMaskRecord,
    Zone,
)
from app.services.l03_ingestion.cache import load_bands
from app.services.l03_ingestion.reader import WindowedBands
from app.services.l05_water_detection.chips import (
    BODY_SCOPE,
    MASK_NODATA,
    MASK_WATER,
    chip_key,
    put_chip,
    read_chip,
)
from app.services.l05_water_detection.service import get_mask
from app.services.l06_indicators.registry import (
    INDICATORS,
    Indicator,
    to_reflectance,
)
from app.services.l06_indicators.zonal import (
    ZoneRejection,
    ZoneStats,
    aggregate_all,
    rasterize_zones,
)

log = logging.getLogger(__name__)

CHIP_NODATA = float("nan")


class MaskMissingError(LookupError):
    """No usable-or-not verdict yet for this (water body, scene): run L4/L5 first."""


class MaskUnusableError(ValueError):
    """The scene was rejected by L4 for this body; nothing to compute."""


@dataclass(frozen=True)
class IndicatorRaster:
    indicator: Indicator
    values: np.ndarray  # float32, NaN outside the aggregation domain, clipped
    clipped: np.ndarray  # bool, pixels outside valid_range before clipping
    clipped_pct: float  # over finite pixels
    n_pixels: int


@dataclass
class IndicatorRunResult:
    water_body_id: str
    computed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already done
    unusable: list[str] = field(default_factory=list)  # mask rejected the scene
    failed: list[str] = field(default_factory=list)


# --- pure computation -------------------------------------------------------------


def compute_indicator_rasters(
    bands: WindowedBands,
    water: np.ndarray,
    valid: np.ndarray,
    *,
    indicators: dict[str, Indicator] | None = None,
    boa_offset: int,
) -> dict[str, IndicatorRaster]:
    """Every registered indicator on the scene grid. Water-only indicators are NaN
    off the water mask; all are NaN on cloud/nodata pixels."""
    indicators = indicators or INDICATORS
    rho = to_reflectance(bands.arrays, offset=boa_offset)
    out: dict[str, IndicatorRaster] = {}
    for key, ind in indicators.items():
        ind.check_bands(rho)
        raw = ind.compute(rho)
        domain = (valid & water) if ind.water_only else valid
        raw = np.where(domain, raw, np.nan).astype(np.float32)
        values, clipped_frac = ind.clip(raw)
        lo, hi = ind.valid_range
        with np.errstate(invalid="ignore"):
            flags = np.isfinite(raw) & ((raw < lo) | (raw > hi))
        out[key] = IndicatorRaster(
            indicator=ind,
            values=values,
            clipped=np.asarray(flags, dtype=bool),
            clipped_pct=round(100.0 * clipped_frac, 2),
            n_pixels=int(np.isfinite(values).sum()),
        )
    return out


def mask_from_chip(store: ObjectStore, key: str) -> tuple[np.ndarray, np.ndarray]:
    """(water, valid) boolean arrays from an S3 water-mask chip."""
    data, _transform, _crs = read_chip(store, key)
    return data == MASK_WATER, data != MASK_NODATA


# --- persistence ------------------------------------------------------------------


def get_run(session: Session, water_body_id: str, scene_id: str) -> IndicatorRun | None:
    return session.execute(
        select(IndicatorRun).where(
            IndicatorRun.water_body_id == water_body_id, IndicatorRun.scene_id == scene_id
        )
    ).scalar_one_or_none()


def _usable_mask(session: Session, water_body_id: str, scene_id: str) -> WaterMaskRecord:
    row = get_mask(session, water_body_id, scene_id)
    if row is None or row.status != "done":
        raise MaskMissingError(f"no water mask for {scene_id} over {water_body_id}")
    if not row.usable or not row.chip_key:
        raise MaskUnusableError(
            f"{scene_id} rejected for {water_body_id} (valid_pixel_pct={row.valid_pixel_pct})"
        )
    return row


def _upsert_chip_row(
    session: Session,
    *,
    water_body_id: str,
    scene_id: str,
    layer: str,
    key: str,
    nbytes: int,
    bounds_4326: tuple[float, float, float, float],
    crs: str,
    meta: dict[str, Any],
) -> None:
    values = {
        "water_body_id": water_body_id,
        "zone_id": None,
        "scene_id": scene_id,
        "layer": layer,
        "s3_key": key,
        "bytes": nbytes,
        "bounds": list(bounds_4326),
        "crs": crs,
        "meta": meta,
    }
    stmt = insert(RasterChip).values(**values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RasterChip.s3_key],
            set_={k: v for k, v in values.items() if k != "s3_key"},
        )
    )


def upsert_observations(
    session: Session,
    *,
    water_body_id: str,
    scene_id: str,
    observed_at: datetime,
    stats: list[ZoneStats],
) -> int:
    if not stats:
        return 0
    rows = [
        {
            "observed_at": observed_at,
            "zone_id": s.zone_id,
            "indicator": s.indicator,
            "scene_id": scene_id,
            "water_body_id": water_body_id,
            "mean": s.mean,
            "p90": s.p90,
            "std": s.std,
            "n_pixels": s.n_pixels,
            "valid_pixel_pct": s.valid_pixel_pct,
            "water_fraction_pct": s.water_fraction_pct,
            "clipped_pct": s.clipped_pct,
        }
        for s in stats
    ]
    stmt = insert(IndicatorObservation).values(rows)
    update_cols = {
        c: getattr(stmt.excluded, c)
        for c in (
            "scene_id",
            "water_body_id",
            "mean",
            "p90",
            "std",
            "n_pixels",
            "valid_pixel_pct",
            "water_fraction_pct",
            "clipped_pct",
        )
    }
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                IndicatorObservation.observed_at,
                IndicatorObservation.zone_id,
                IndicatorObservation.indicator,
            ],
            set_=update_cols,
        )
    )
    return len(rows)


def _rejection_dicts(rejected: list[ZoneRejection]) -> list[dict[str, Any]]:
    return [
        {
            "zone_id": r.zone_id,
            "indicator": r.indicator,
            "reason": r.reason,
            "valid_pixel_pct": r.valid_pixel_pct,
            "n_pixels": r.n_pixels,
        }
        for r in rejected
    ]


# --- orchestration ----------------------------------------------------------------


def compute_indicators(
    session: Session,
    store: ObjectStore,
    wb: WaterBody,
    scene: Scene,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> tuple[IndicatorRun, bool]:
    """Run L6 for one scene over one water body. Returns (run row, did_work)."""
    settings = settings or get_settings()
    run = get_run(session, wb.id, scene.id)
    if run is not None and not force:
        if run.status == "done" and all(
            store.exists(c["chip_key"]) for c in run.chips.values() if c.get("chip_key")
        ):
            return run, False
        if run.status == "skipped":
            return run, False

    if run is None:
        run = IndicatorRun(
            water_body_id=wb.id, scene_id=scene.id, sensed_at=scene.sensed_at, status="pending"
        )
        session.add(run)
        session.flush()

    started = time.perf_counter()
    try:
        try:
            mask_row = _usable_mask(session, wb.id, scene.id)
        except MaskUnusableError as exc:
            run.status = "skipped"
            run.error = str(exc)[:2000]
            run.n_zones = run.n_observations = 0
            run.rejected, run.chips = [], {}
            run.computed_at = datetime.now(UTC)
            session.flush()
            log.info("indicators skipped: mask unusable", extra={"scene_id": scene.id})
            return run, True

        assert mask_row.chip_key is not None
        ingestion_key = _ingestion_cache_key(session, wb.id, scene.id)
        bands = load_bands(store, ingestion_key)
        water, valid = mask_from_chip(store, mask_row.chip_key)
        if water.shape != bands.shape:
            raise ValueError(
                f"mask chip {mask_row.chip_key} shape {water.shape} != bands {bands.shape}"
            )

        rasters = compute_indicator_rasters(
            bands, water, valid, boa_offset=settings.s2_boa_add_offset
        )
        zones = session.scalars(
            select(Zone).where(Zone.water_body_id == wb.id).order_by(Zone.seq)
        ).all()
        zone_rasters = rasterize_zones(
            ((z.id, to_shape(z.geom)) for z in zones), bands.crs, bands.transform, valid, water
        )
        stats, rejected = aggregate_all(
            zone_rasters,
            {k: (r.indicator, r.values, r.clipped) for k, r in rasters.items()},
            min_valid_pct=settings.indicator_min_valid_pct,
            min_pixels=settings.indicator_min_pixels,
        )

        chips: dict[str, Any] = {}
        scene_date = scene.sensed_at.date()
        for key, r in rasters.items():
            ck = chip_key(wb.id, scene_date, key, BODY_SCOPE, settings.chip_prefix)
            info = put_chip(
                store,
                ck,
                r.values,
                bands.transform,
                bands.crs,
                bands.bounds,
                nodata=CHIP_NODATA,
                tags={
                    "scene_id": scene.id,
                    "water_body_id": wb.id,
                    "indicator": key,
                    "display_name": r.indicator.display_name,
                    "formula": r.indicator.formula_doc,
                    "valid_range": f"{r.indicator.valid_range[0]},{r.indicator.valid_range[1]}",
                    "clipped_pct": str(r.clipped_pct),
                    "boa_add_offset": str(settings.s2_boa_add_offset),
                    "water_only": str(r.indicator.water_only),
                },
            )
            _upsert_chip_row(
                session,
                water_body_id=wb.id,
                scene_id=scene.id,
                layer=key,
                key=info.key,
                nbytes=info.bytes,
                bounds_4326=info.bounds_4326,
                crs=bands.crs,
                meta={
                    "sensed_at": scene.sensed_at.isoformat(),
                    "indicator": key,
                    "valid_range": list(r.indicator.valid_range),
                    "clipped_pct": r.clipped_pct,
                    "n_pixels": r.n_pixels,
                    "water_only": r.indicator.water_only,
                },
            )
            chips[key] = {
                "chip_key": info.key,
                "clipped_pct": r.clipped_pct,
                "n_pixels": r.n_pixels,
            }

        n_obs = upsert_observations(
            session,
            water_body_id=wb.id,
            scene_id=scene.id,
            observed_at=scene.sensed_at,
            stats=stats,
        )
        run.n_zones = len(zones)
        run.n_observations = n_obs
        run.rejected = _rejection_dicts(rejected)
        metrics.zones_processed.labels(outcome="accepted").inc(n_obs)
        metrics.zones_processed.labels(outcome="rejected").inc(len(rejected))
        run.chips = chips
        run.boa_offset = settings.s2_boa_add_offset
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
        "indicators computed",
        extra={
            "water_body_id": wb.id,
            "scene_id": scene.id,
            "n_zones": run.n_zones,
            "n_observations": n_obs,
            "n_rejected": len(rejected),
            "chips": len(chips),
            "duration_s": run.duration_s,
        },
    )
    return run, True


def _ingestion_cache_key(session: Session, water_body_id: str, scene_id: str) -> str:
    key = session.execute(
        select(SceneIngestion.cache_key).where(
            SceneIngestion.water_body_id == water_body_id,
            SceneIngestion.scene_id == scene_id,
            SceneIngestion.status == "done",
        )
    ).scalar_one_or_none()
    if not key:
        raise MaskMissingError(f"{scene_id} bands are not cached for {water_body_id}")
    return str(key)


def masked_scenes(
    session: Session, water_body_id: str, date_from: date, date_to: date
) -> list[Scene]:
    """Scenes with a finished L4/L5 verdict for the body, sensed within the window."""
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, 23, 59, 59, tzinfo=UTC)
    stmt = (
        select(Scene)
        .join(WaterMaskRecord, WaterMaskRecord.scene_id == Scene.id)
        .where(
            WaterMaskRecord.water_body_id == water_body_id,
            WaterMaskRecord.status == "done",
            Scene.sensed_at >= start,
            Scene.sensed_at <= end,
        )
        .order_by(Scene.sensed_at)
    )
    return list(session.scalars(stmt).all())


def scenes_needing_indicators(
    session: Session, water_body_id: str, scene_ids: list[str]
) -> list[str]:
    if not scene_ids:
        return []
    done = set(
        session.scalars(
            select(IndicatorRun.scene_id).where(
                IndicatorRun.water_body_id == water_body_id,
                IndicatorRun.scene_id.in_(scene_ids),
                IndicatorRun.status.in_(["done", "skipped"]),
            )
        ).all()
    )
    return [s for s in scene_ids if s not in done]


def process_water_body(
    session: Session,
    store: ObjectStore,
    water_body_id: str,
    date_from: date,
    date_to: date | None = None,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> IndicatorRunResult:
    """Backfill indicators for every masked scene of a body in the window."""
    settings = settings or get_settings()
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    result = IndicatorRunResult(water_body_id=water_body_id)
    for scene in masked_scenes(session, water_body_id, date_from, date_to or date_from):
        try:
            run, did_work = compute_indicators(
                session, store, wb, scene, settings=settings, force=force
            )
        except Exception:
            result.failed.append(scene.id)
            log.exception("indicators failed", extra={"scene_id": scene.id})
            raise
        if not did_work:
            result.skipped.append(scene.id)
        elif run.status == "skipped":
            result.unusable.append(scene.id)
        else:
            result.computed.append(scene.id)
    return result
