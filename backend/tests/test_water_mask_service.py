"""L4+L5 service tests. All need Postgres (integration); the acceptance test also
hits Earth Search and MinIO for real."""

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select, text

from app.core.storage import MemoryStore
from app.db.models import RasterChip, Scene, SceneIngestion, WaterBody, WaterMaskRecord
from app.services.l03_ingestion import service as ingest_svc
from app.services.l03_ingestion.cache import cache_key, save_bands
from app.services.l05_water_detection import service as svc
from app.services.l05_water_detection.chips import read_chip
from app.workers.tasks import compute_water_mask as mask_task
from tests.test_ingestion_service import _cand
from tests.test_preprocessing import make_bands
from tests.test_water_mask import LAND, WATER_PX

pytestmark = pytest.mark.integration

WB = "wb_khadakwasla"


@pytest.fixture
def session():  # type: ignore[no-untyped-def]
    from app.db.sync_session import sync_session

    def _clean(s: Any) -> None:
        s.execute(text("DELETE FROM raster_chips WHERE scene_id LIKE 'TESTM_%'"))
        s.execute(text("DELETE FROM water_masks WHERE scene_id LIKE 'TESTM_%'"))
        s.execute(text("DELETE FROM scene_ingestions WHERE scene_id LIKE 'TESTM_%'"))
        s.execute(text("DELETE FROM scenes WHERE id LIKE 'TESTM_%'"))
        s.commit()

    with sync_session() as s:
        _clean(s)
        yield s
        s.rollback()
        _clean(s)


def _seed_ingested(  # type: ignore[no-untyped-def]
    session: Any, store: MemoryStore, scene_id: str, day: date, scl_fill: int
):
    """Insert a scenes row + done ingestion whose cached bands cover the real
    Khadakwasla window with uniform synthetic values (SCL = scl_fill)."""
    from geoalchemy2.shape import to_shape
    from pyproj import CRS

    from app.services.l03_ingestion.reader import BUFFER_M, _aoi_bounds_in

    wb = session.get(WaterBody, WB)
    geom = to_shape(wb.geom)
    minx, miny, maxx, maxy = _aoi_bounds_in(CRS.from_epsg(32643), geom, BUFFER_M)
    from affine import Affine

    width, height = int((maxx - minx) // 10), int((maxy - miny) // 10)
    shape = (height, width)
    arrays = {
        b: np.full(shape, v, dtype=np.uint16)
        for b, v in (("B03", 700), ("B04", 500), ("B05", 400), ("B08", 300), ("B11", 150))
    }
    scl = np.full(shape, scl_fill, dtype=np.uint8)
    bands = make_bands(scl, **arrays)
    bands.scene_id, bands.water_body_id = scene_id, WB
    bands.transform = Affine(10, 0, minx, 0, -10, maxy)
    bands.bounds = (minx, maxy - height * 10, minx + width * 10, maxy)
    bands.arrays = {**arrays, "SCL": scl}

    scene = ingest_svc.upsert_scene(session, _cand(scene_id, day, 5.0), 60.0)
    key = cache_key("cache", WB, scene_id)
    save_bands(store, key, bands)
    session.add(
        SceneIngestion(
            water_body_id=WB, scene_id=scene_id, status="done", cache_key=key, bytes_read=1
        )
    )
    session.flush()
    return wb, scene, geom, LAND, WATER_PX


def test_mask_requires_ingestion(session) -> None:  # type: ignore[no-untyped-def]
    wb = session.get(WaterBody, WB)
    scene = ingest_svc.upsert_scene(session, _cand("TESTM_NOING", date(2026, 5, 3), 5.0), 60.0)
    with pytest.raises(svc.IngestionMissingError):
        svc.compute_water_mask(session, MemoryStore(), wb, scene)
    session.commit()
    row = svc.get_mask(session, WB, "TESTM_NOING")
    assert row is not None and row.status == "failed" and "not ingested" in (row.error or "")


def test_cloudy_scene_is_rejected_without_chip(session) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore()
    wb, scene, *_ = _seed_ingested(session, store, "TESTM_CLOUD", date(2026, 7, 20), scl_fill=9)
    row, did_work = svc.compute_water_mask(session, store, wb, scene)
    session.commit()
    assert did_work and row.status == "done" and not row.usable
    assert row.valid_pixel_pct == 0.0 and row.chip_key is None and row.water_extent_km2 is None
    assert not [k for k in store.objects if k.startswith("chips/")]
    # Idempotent: a second call is a no-op.
    assert svc.compute_water_mask(session, store, wb, scene)[1] is False


def test_clear_scene_gets_mask_chip_and_extent(session) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore()
    wb, scene, *_ = _seed_ingested(session, store, "TESTM_CLEAR", date(2026, 5, 3), scl_fill=6)
    row, did_work = svc.compute_water_mask(session, store, wb, scene)
    session.commit()
    assert did_work and row.usable and row.valid_pixel_pct == 100.0
    # Uniform "all water" window: Otsu falls back to 0.0 and everything inside the
    # buffered polygon is water, so the extent tracks the registered area.
    assert row.threshold_method == "fallback"
    assert row.water_extent_km2 is not None and row.water_extent_km2 == pytest.approx(
        wb.area_km2, rel=0.08
    )
    assert row.chip_key == f"chips/{WB}/2026-05-03/body/watermask.tif"
    chip = session.execute(select(RasterChip).where(RasterChip.s3_key == row.chip_key)).scalar_one()
    assert chip.layer == "watermask" and chip.zone_id is None and chip.crs == "EPSG:32643"
    lon0, lat0, lon1, lat1 = chip.bounds
    assert 73 < lon0 < lon1 < 74 and 18 < lat0 < lat1 < 19
    data, _t, _c = read_chip(store, row.chip_key)
    assert int((data == 1).sum()) == row.water_pixels

    # Re-run is a no-op; force recomputes and keeps a single chip row.
    assert svc.compute_water_mask(session, store, wb, scene)[1] is False
    assert svc.compute_water_mask(session, store, wb, scene, force=True)[1] is True
    session.commit()
    n = session.execute(
        text("SELECT count(*) FROM raster_chips WHERE scene_id = 'TESTM_CLEAR'")
    ).scalar_one()
    assert n == 1


def test_process_water_body_window(session) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore()
    _seed_ingested(session, store, "TESTM_W1", date(2026, 5, 3), scl_fill=6)
    _seed_ingested(session, store, "TESTM_W2", date(2026, 5, 8), scl_fill=9)
    result = svc.process_water_body(session, store, WB, date(2026, 5, 1), date(2026, 5, 10))
    session.commit()
    assert result.computed == ["TESTM_W1"] and result.rejected == ["TESTM_W2"]
    again = svc.process_water_body(session, store, WB, date(2026, 5, 1), date(2026, 5, 10))
    assert sorted(again.skipped) == ["TESTM_W1", "TESTM_W2"]
    assert svc.scenes_needing_mask(session, WB, ["TESTM_W1", "TESTM_W2", "TESTM_X"]) == ["TESTM_X"]


def test_celery_task_configuration() -> None:
    assert mask_task.queue == "processing"
    assert svc.IngestionMissingError in mask_task.autoretry_for
    assert mask_task.max_retries == 4 and mask_task.acks_late is True


# --- S3 acceptance: summer draw-down vs post-monsoon full pool on Khadakwasla ------


def _first_usable_mask(session: Any, store: Any, d0: date, d1: date) -> WaterMaskRecord:
    ingest_svc.ingest_water_body(session, store, WB, d0, date_to=d1)
    session.commit()
    result = svc.process_water_body(session, store, WB, d0, d1)
    session.commit()
    rows = session.scalars(
        select(WaterMaskRecord)
        .where(
            WaterMaskRecord.water_body_id == WB,
            WaterMaskRecord.usable.is_(True),
            WaterMaskRecord.sensed_at >= datetime(d0.year, d0.month, d0.day, tzinfo=UTC),
            WaterMaskRecord.sensed_at <= datetime(d1.year, d1.month, d1.day, 23, 59, tzinfo=UTC),
        )
        .order_by(WaterMaskRecord.valid_pixel_pct.desc())
    ).all()
    assert rows, f"no usable scene in {d0}..{d1}: {result}"
    row: WaterMaskRecord = rows[0]
    return row


def test_acceptance_khadakwasla_seasonal_extent(session) -> None:  # type: ignore[no-untyped-def]
    """Real Earth Search + MinIO. Late-summer draw-down (May 2026) must show
    measurably less visible water than the post-monsoon full pool (Oct-Nov 2025)."""
    from app.core.storage import get_store

    store = get_store()
    summer = _first_usable_mask(session, store, date(2026, 5, 1), date(2026, 5, 10))
    monsoon = _first_usable_mask(session, store, date(2025, 10, 20), date(2025, 11, 30))

    for row in (summer, monsoon):
        assert row.chip_key and store.exists(row.chip_key)
        assert row.valid_pixel_pct is not None and row.valid_pixel_pct >= 40
        assert row.water_extent_km2 is not None and row.water_extent_km2 > 0
        scene = session.get(Scene, row.scene_id)
        assert scene is not None and "43QCA" in scene.id

    wb = session.get(WaterBody, WB)
    s_km2, m_km2 = summer.water_extent_km2 or 0.0, monsoon.water_extent_km2 or 0.0
    assert s_km2 < wb.area_km2 * 1.1
    assert m_km2 > s_km2 * 1.15, (s_km2, m_km2)
