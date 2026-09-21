"""L4 + L5 orchestration: cached bands -> validity verdict -> water mask -> COG chip.

Idempotent on (water_body_id, scene_id) like ingestion. A scene the L4 step rejects
(valid_pixel_pct below the threshold) gets a ``water_masks`` row with
``usable = false`` and no chip; downstream layers must filter on that flag.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import RasterChip, Scene, SceneIngestion, WaterBody, WaterMaskRecord
from app.services.l03_ingestion.cache import load_bands
from app.services.l04_preprocessing.masks import preprocess
from app.services.l05_water_detection.chips import (
    BODY_SCOPE,
    MASK_NODATA,
    chip_key,
    encode_water_mask,
    put_chip,
)
from app.services.l05_water_detection.mask import detect_water

log = logging.getLogger(__name__)

LAYER_WATERMASK = "watermask"


class IngestionMissingError(LookupError):
    """The scene's bands are not cached for this water body yet (run L3 first)."""


@dataclass
class MaskRunResult:
    water_body_id: str
    computed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # L4 said not usable
    skipped: list[str] = field(default_factory=list)  # already done
    failed: list[str] = field(default_factory=list)


def get_mask(session: Session, water_body_id: str, scene_id: str) -> WaterMaskRecord | None:
    return session.execute(
        select(WaterMaskRecord).where(
            WaterMaskRecord.water_body_id == water_body_id, WaterMaskRecord.scene_id == scene_id
        )
    ).scalar_one_or_none()


def _done_ingestion(session: Session, water_body_id: str, scene_id: str) -> SceneIngestion:
    row = session.execute(
        select(SceneIngestion).where(
            SceneIngestion.water_body_id == water_body_id,
            SceneIngestion.scene_id == scene_id,
            SceneIngestion.status == "done",
        )
    ).scalar_one_or_none()
    if row is None or not row.cache_key:
        raise IngestionMissingError(f"{scene_id} is not ingested for {water_body_id}")
    return row


def _upsert_chip(
    session: Session,
    *,
    water_body_id: str,
    scene_id: str,
    layer: str,
    key: str,
    nbytes: int,
    bounds_4326: tuple[float, float, float, float],
    crs: str,
    meta: dict[str, object],
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
    stmt = stmt.on_conflict_do_update(
        index_elements=[RasterChip.s3_key],
        set_={k: v for k, v in values.items() if k != "s3_key"},
    )
    session.execute(stmt)


def compute_water_mask(
    session: Session,
    store: ObjectStore,
    wb: WaterBody,
    scene: Scene,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> tuple[WaterMaskRecord, bool]:
    """Run L4 + L5 for one cached scene over one water body. Returns (row, did_work)."""
    settings = settings or get_settings()
    row = get_mask(session, wb.id, scene.id)
    if row is not None and row.status == "done" and not force:
        chip_ok = not row.usable or (row.chip_key is not None and store.exists(row.chip_key))
        if chip_ok:
            return row, False

    ingestion = _done_ingestion(session, wb.id, scene.id)
    if row is None:
        row = WaterMaskRecord(
            water_body_id=wb.id, scene_id=scene.id, sensed_at=scene.sensed_at, status="pending"
        )
        session.add(row)
        session.flush()

    started = time.perf_counter()
    try:
        assert ingestion.cache_key is not None
        bands = load_bands(store, ingestion.cache_key)
        geom = to_shape(wb.geom)
        pre = preprocess(bands, geom, min_valid_pct=settings.mask_min_valid_pct)
        row.aoi_pixels = pre.aoi_pixels
        row.valid_pixel_pct = pre.valid_pixel_pct
        row.cloud_pixel_pct = pre.cloud_pixel_pct
        row.usable = pre.usable

        if not pre.usable:
            metrics.scenes_rejected_cloud.labels(stage="mask").inc()
            for attr in (
                "mndwi_threshold",
                "threshold_method",
                "water_pixels",
                "water_extent_km2",
                "water_fraction_pct",
                "n_components",
                "chip_key",
            ):
                setattr(row, attr, None)
            log.info(
                "scene rejected for water body",
                extra={
                    "water_body_id": wb.id,
                    "scene_id": scene.id,
                    "valid_pixel_pct": pre.valid_pixel_pct,
                    "min_valid_pct": settings.mask_min_valid_pct,
                },
            )
        else:
            mask = detect_water(bands, pre, geom)
            key = chip_key(
                wb.id, scene.sensed_at.date(), LAYER_WATERMASK, BODY_SCOPE, settings.chip_prefix
            )
            info = put_chip(
                store,
                key,
                encode_water_mask(mask.water, pre.valid),
                bands.transform,
                bands.crs,
                bands.bounds,
                nodata=MASK_NODATA,
                tags={
                    "scene_id": scene.id,
                    "water_body_id": wb.id,
                    "mndwi_threshold": str(mask.threshold),
                    "threshold_method": mask.threshold_method,
                    "encoding": "0=land 1=water 255=cloud/nodata",
                },
            )
            _upsert_chip(
                session,
                water_body_id=wb.id,
                scene_id=scene.id,
                layer=LAYER_WATERMASK,
                key=info.key,
                nbytes=info.bytes,
                bounds_4326=info.bounds_4326,
                crs=bands.crs,
                meta={
                    "sensed_at": scene.sensed_at.isoformat(),
                    "water_extent_km2": mask.water_extent_km2,
                    "valid_pixel_pct": pre.valid_pixel_pct,
                },
            )
            row.mndwi_threshold = mask.threshold
            row.threshold_method = mask.threshold_method
            row.water_pixels = mask.water_pixels
            row.water_extent_km2 = mask.water_extent_km2
            row.water_fraction_pct = mask.water_fraction_pct
            row.n_components = mask.n_components
            row.chip_key = info.key
            log.info(
                "water mask computed",
                extra={
                    "water_body_id": wb.id,
                    "scene_id": scene.id,
                    "water_extent_km2": mask.water_extent_km2,
                    "valid_pixel_pct": pre.valid_pixel_pct,
                    "threshold": mask.threshold,
                    "method": mask.threshold_method,
                    "chip_bytes": info.bytes,
                },
            )
    except Exception as exc:
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:2000]
        session.flush()
        raise

    row.status = "done"
    row.error = None
    row.duration_s = round(time.perf_counter() - started, 3)
    row.computed_at = datetime.now(UTC)
    session.flush()
    return row, True


def ingested_scenes(
    session: Session, water_body_id: str, date_from: date, date_to: date
) -> list[Scene]:
    """Scenes with cached bands for the body, sensed within [date_from, date_to]."""
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
    stmt = (
        select(Scene)
        .join(SceneIngestion, SceneIngestion.scene_id == Scene.id)
        .where(
            SceneIngestion.water_body_id == water_body_id,
            SceneIngestion.status == "done",
            Scene.sensed_at >= start,
            Scene.sensed_at < end,
        )
        .order_by(Scene.sensed_at)
    )
    return list(session.scalars(stmt).all())


def scenes_needing_mask(session: Session, water_body_id: str, scene_ids: list[str]) -> list[str]:
    if not scene_ids:
        return []
    done = set(
        session.scalars(
            select(WaterMaskRecord.scene_id).where(
                WaterMaskRecord.water_body_id == water_body_id,
                WaterMaskRecord.scene_id.in_(scene_ids),
                WaterMaskRecord.status == "done",
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
) -> MaskRunResult:
    """Compute masks for every ingested scene of a body in the window."""
    settings = settings or get_settings()
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    result = MaskRunResult(water_body_id=water_body_id)
    for scene in ingested_scenes(session, water_body_id, date_from, date_to or date_from):
        try:
            row, did_work = compute_water_mask(
                session, store, wb, scene, settings=settings, force=force
            )
        except Exception:
            result.failed.append(scene.id)
            log.exception("water mask failed", extra={"scene_id": scene.id})
            raise
        if not did_work:
            result.skipped.append(scene.id)
        elif row.usable:
            result.computed.append(scene.id)
        else:
            result.rejected.append(scene.id)
    return result
