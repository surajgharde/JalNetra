"""L3 ingestion service: discover scenes for a water body, persist ``scenes`` rows,
read the windowed bands once and cache them. Idempotent on (water_body_id, scene_id).
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
from app.db.models import Scene, SceneIngestion, WaterBody
from app.services.l03_ingestion.cache import cache_key, save_bands
from app.services.l03_ingestion.reader import read_windowed_bands
from app.services.l03_ingestion.stac import SceneCandidate, STACSource, build_source

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    water_body_id: str
    scenes_found: int = 0
    ingested: list[str] = field(default_factory=list)  # scene ids read this run
    skipped: list[str] = field(default_factory=list)  # already cached (no-op)
    unusable: list[str] = field(default_factory=list)  # over the cloud threshold
    failed: list[str] = field(default_factory=list)


def _get_water_body(session: Session, water_body_id: str) -> WaterBody:
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    return wb


def upsert_scene(session: Session, cand: SceneCandidate, max_cloud_pct: float) -> Scene:
    values = {
        "id": cand.id,
        "mgrs_tile": cand.mgrs_tile,
        "sensed_at": cand.sensed_at,
        "platform": cand.platform,
        "cloud_pct": cand.cloud_pct,
        "stac_href": cand.stac_href,
        "source": cand.source,
        "usable": cand.cloud_pct <= max_cloud_pct,
        "assets": cand.assets,
        "epsg": cand.epsg,
        "boa_add_offset": cand.boa_add_offset,
    }
    stmt = insert(Scene).values(**values)
    # A re-search never downgrades provenance: keep the first source that served the scene.
    stmt = stmt.on_conflict_do_update(
        index_elements=[Scene.id],
        set_={k: v for k, v in values.items() if k not in ("id", "source", "assets")},
    )
    session.execute(stmt)
    scene = session.get(Scene, cand.id)
    assert scene is not None
    return scene


def search_scenes(
    session: Session,
    water_body_id: str,
    date_from: date,
    date_to: date,
    *,
    max_cloud_pct: float | None = None,
    source: STACSource | None = None,
    settings: Settings | None = None,
) -> list[Scene]:
    """Query the STAC source for the body's tiles and persist one ``scenes`` row per pass.
    Every pass is stored; ``usable`` reflects the cloud threshold."""
    settings = settings or get_settings()
    source = source or build_source(settings)
    threshold = settings.stac_max_cloud_pct if max_cloud_pct is None else max_cloud_pct
    wb = _get_water_body(session, water_body_id)
    geom = to_shape(wb.geom)
    candidates = source.search(geom, date_from, date_to, tiles=wb.mgrs_tiles or None)
    scenes = [upsert_scene(session, c, threshold) for c in candidates]
    session.flush()
    log.info(
        "scenes searched",
        extra={
            "water_body_id": water_body_id,
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "found": len(scenes),
            "usable": sum(s.usable for s in scenes),
        },
    )
    return sorted(scenes, key=lambda s: s.sensed_at)


def candidate_from_scene(scene: Scene, gdal_env: dict[str, str] | None = None) -> SceneCandidate:
    return SceneCandidate(
        id=scene.id,
        mgrs_tile=scene.mgrs_tile,
        sensed_at=scene.sensed_at,
        cloud_pct=scene.cloud_pct,
        platform=scene.platform,
        stac_href=scene.stac_href,
        source=scene.source,
        assets=dict(scene.assets),
        epsg=scene.epsg,
        boa_add_offset=scene.boa_add_offset,
        gdal_env=gdal_env or {},
    )


def get_ingestion(session: Session, water_body_id: str, scene_id: str) -> SceneIngestion | None:
    return session.execute(
        select(SceneIngestion).where(
            SceneIngestion.water_body_id == water_body_id, SceneIngestion.scene_id == scene_id
        )
    ).scalar_one_or_none()


def ingest_scene(
    session: Session,
    store: ObjectStore,
    wb: WaterBody,
    scene: Scene,
    *,
    gdal_env: dict[str, str] | None = None,
    settings: Settings | None = None,
) -> tuple[SceneIngestion, bool]:
    """Read + cache one scene over one water body. Returns (row, did_work)."""
    settings = settings or get_settings()
    key = cache_key(settings.ingest_cache_prefix, wb.id, scene.id)
    row = get_ingestion(session, wb.id, scene.id)
    if row is not None and row.status == "done" and store.exists(key):
        log.info("scene already ingested", extra={"water_body_id": wb.id, "scene_id": scene.id})
        return row, False

    if row is None:
        row = SceneIngestion(water_body_id=wb.id, scene_id=scene.id, status="pending")
        session.add(row)
        session.flush()

    started = time.perf_counter()
    try:
        bands = read_windowed_bands(candidate_from_scene(scene, gdal_env), to_shape(wb.geom), wb.id)
        save_bands(store, key, bands)
    except Exception as exc:
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:2000]
        session.flush()
        raise
    row.status = "done"
    row.cache_key = key
    row.bytes_read = bands.bytes_read
    row.duration_s = round(time.perf_counter() - started, 3)
    row.source = scene.source
    row.error = None
    row.ingested_at = datetime.now(UTC)
    session.flush()
    return row, True


def ingest_water_body(
    session: Session,
    store: ObjectStore,
    water_body_id: str,
    day: date,
    *,
    date_to: date | None = None,
    source: STACSource | None = None,
    settings: Settings | None = None,
) -> IngestResult:
    """Search [day, date_to or day] and ingest every usable scene not yet cached."""
    settings = settings or get_settings()
    source = source or build_source(settings)
    result = IngestResult(water_body_id=water_body_id)
    wb = _get_water_body(session, water_body_id)
    scenes = search_scenes(
        session, water_body_id, day, date_to or day, source=source, settings=settings
    )
    result.scenes_found = len(scenes)
    for scene in scenes:
        if not scene.usable:
            result.unusable.append(scene.id)
            metrics.scenes_rejected_cloud.labels(stage="stac").inc()
            continue
        try:
            _row, did_work = ingest_scene(
                session, store, wb, scene, gdal_env=source.gdal_env(), settings=settings
            )
        except Exception:
            result.failed.append(scene.id)
            log.exception("scene ingest failed", extra={"scene_id": scene.id})
            raise
        (result.ingested if did_work else result.skipped).append(scene.id)
        if did_work:
            metrics.scenes_ingested.labels(source=scene.source).inc()
    return result


def pending_scenes_for_tier(
    session: Session, tier: int, lookback_days: int, *, source: STACSource | None = None
) -> list[tuple[str, date]]:
    """(water_body_id, date) pairs with a usable scene in the window but no finished ingestion."""
    settings = get_settings()
    source = source or build_source(settings)
    today = datetime.now(UTC).date()
    since = today - timedelta(days=lookback_days)
    pairs: list[tuple[str, date]] = []
    for wb_id in session.scalars(select(WaterBody.id).where(WaterBody.tier == tier)).all():
        scenes = search_scenes(session, wb_id, since, today, source=source, settings=settings)
        for scene in scenes:
            if not scene.usable:
                continue
            row = get_ingestion(session, wb_id, scene.id)
            if row is None or row.status != "done":
                pairs.append((wb_id, scene.sensed_at.date()))
    return sorted(set(pairs))
