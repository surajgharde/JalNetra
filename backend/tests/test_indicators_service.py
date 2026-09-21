"""L6 service tests. All need Postgres (integration); the acceptance test also
hits Earth Search and MinIO for real."""

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select, text

from app.core.storage import MemoryStore
from app.db.models import IndicatorObservation, IndicatorRun, RasterChip, WaterBody, Zone
from app.services.l03_ingestion import service as ingest_svc
from app.services.l05_water_detection import service as mask_svc
from app.services.l05_water_detection.chips import read_chip
from app.services.l06_indicators import service as svc
from app.services.l06_indicators.registry import INDICATORS, QUALITY_INDICATOR_KEYS
from app.workers.tasks import compute_indicators as ind_task
from tests.test_indicators import CLEAR, dn
from tests.test_water_mask_service import WB, _seed_ingested

pytestmark = pytest.mark.integration


@pytest.fixture
def session():  # type: ignore[no-untyped-def]
    from app.db.sync_session import sync_session

    def _clean(s: Any) -> None:
        for table in (
            "indicator_observations",
            "indicator_runs",
            "raster_chips",
            "water_masks",
            "scene_ingestions",
        ):
            s.execute(text(f"DELETE FROM {table} WHERE scene_id LIKE 'TESTI_%'"))
        s.execute(text("DELETE FROM scenes WHERE id LIKE 'TESTI_%'"))
        s.commit()

    with sync_session() as s:
        _clean(s)
        yield s
        s.rollback()
        _clean(s)


def _seed_masked(session: Any, store: MemoryStore, scene_id: str, day: date, scl_fill: int = 6):  # type: ignore[no-untyped-def]
    """Ingested + masked scene with uniform *clear-water* reflectance over the
    real Khadakwasla window (so every zone is fully water when scl_fill = 6)."""
    wb, scene, *_ = _seed_ingested(session, store, scene_id, day, scl_fill=scl_fill)
    # _seed_ingested writes dark DN values; overwrite the cache with realistic ones.
    from app.services.l03_ingestion.cache import cache_key, load_bands, save_bands

    key = cache_key("cache", WB, scene_id)
    bands = load_bands(store, key)
    for b, rho in CLEAR.items():
        bands.arrays[b] = np.full(bands.shape, dn(rho), dtype=np.uint16)
    save_bands(store, key, bands)
    mask_svc.compute_water_mask(session, store, wb, scene)
    session.flush()
    return wb, scene


def test_indicators_require_mask(session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_ingestion_service import _cand

    wb = session.get(WaterBody, WB)
    scene = ingest_svc.upsert_scene(session, _cand("TESTI_NOMASK", date(2026, 5, 3), 5.0), 60.0)
    with pytest.raises(svc.MaskMissingError):
        svc.compute_indicators(session, MemoryStore(), wb, scene)
    session.commit()
    run = svc.get_run(session, WB, "TESTI_NOMASK")
    assert run is not None and run.status == "failed" and "no water mask" in (run.error or "")


def test_unusable_scene_is_skipped(session) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore()
    wb, scene = _seed_masked(session, store, "TESTI_CLOUD", date(2026, 7, 20), scl_fill=9)
    run, did_work = svc.compute_indicators(session, store, wb, scene)
    session.commit()
    assert did_work and run.status == "skipped" and run.n_observations == 0
    assert not [k for k in store.objects if "/ndti_turbidity" in k]
    assert svc.compute_indicators(session, store, wb, scene)[1] is False
    n = session.execute(
        text("SELECT count(*) FROM indicator_observations WHERE scene_id = 'TESTI_CLOUD'")
    ).scalar_one()
    assert n == 0


def test_clear_scene_fills_hypertable_and_chips(session) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore()
    wb, scene = _seed_masked(session, store, "TESTI_CLEAR", date(2026, 5, 3))
    run, did_work = svc.compute_indicators(session, store, wb, scene)
    session.commit()

    zones = session.scalars(select(Zone).where(Zone.water_body_id == WB)).all()
    assert did_work and run.status == "done" and run.n_zones == len(zones) >= 4
    assert run.boa_offset == -1000

    # 5 indicators x N zones, every zone fully water and cloud-free.
    obs = session.scalars(
        select(IndicatorObservation).where(IndicatorObservation.scene_id == "TESTI_CLEAR")
    ).all()
    assert len(obs) == len(zones) * len(INDICATORS) == run.n_observations
    assert run.rejected == []
    by_key = {(o.zone_id, o.indicator): o for o in obs}
    z0 = zones[0].id
    ndti = by_key[(z0, "ndti_turbidity")]
    assert ndti.observed_at == scene.sensed_at and ndti.water_body_id == WB
    assert ndti.mean == pytest.approx((0.03 - 0.05) / 0.08, abs=1e-3)
    assert ndti.p90 == pytest.approx(ndti.mean, abs=1e-3) and ndti.std == pytest.approx(0, abs=1e-4)
    assert ndti.valid_pixel_pct == 100.0 and ndti.water_fraction_pct == pytest.approx(100, abs=1)
    assert ndti.n_pixels > 25 and ndti.clipped_pct == 0.0
    assert by_key[(z0, "mndwi_extent")].mean > 0.5

    # One chip per indicator at the body scope, registered in raster_chips.
    for key in INDICATORS:
        ck = f"chips/{WB}/2026-05-03/body/{key}.tif"
        assert run.chips[key]["chip_key"] == ck and store.exists(ck)
        chip = session.execute(select(RasterChip).where(RasterChip.s3_key == ck)).scalar_one()
        assert chip.layer == key and chip.zone_id is None and chip.meta["indicator"] == key
        data, _t, crs = read_chip(store, ck)
        assert crs == "EPSG:32643" and data.dtype == np.float32
        assert np.isfinite(data).sum() == run.chips[key]["n_pixels"]
    sed, _, _ = read_chip(store, run.chips["sediment_proxy"]["chip_key"])
    assert np.nanmean(sed) == pytest.approx(0.03, abs=1e-3)

    # Hypertable really is one: TimescaleDB lists it.
    ht = session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'indicator_observations'"
        )
    ).scalar_one()
    assert ht == 1

    # Idempotent; force recomputes without duplicating rows.
    assert svc.compute_indicators(session, store, wb, scene)[1] is False
    assert svc.compute_indicators(session, store, wb, scene, force=True)[1] is True
    session.commit()
    n_obs = session.execute(
        text("SELECT count(*) FROM indicator_observations WHERE scene_id = 'TESTI_CLEAR'")
    ).scalar_one()
    n_chips = session.execute(
        text("SELECT count(*) FROM raster_chips WHERE scene_id = 'TESTI_CLEAR'")
    ).scalar_one()
    assert n_obs == len(obs) and n_chips == len(INDICATORS) + 1  # + the water mask


def test_process_window_and_needing(session) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore()
    _seed_masked(session, store, "TESTI_W1", date(2026, 5, 3))
    _seed_masked(session, store, "TESTI_W2", date(2026, 5, 8), scl_fill=9)
    result = svc.process_water_body(session, store, WB, date(2026, 5, 1), date(2026, 5, 10))
    session.commit()
    assert result.computed == ["TESTI_W1"] and result.unusable == ["TESTI_W2"]
    again = svc.process_water_body(session, store, WB, date(2026, 5, 1), date(2026, 5, 10))
    assert sorted(again.skipped) == ["TESTI_W1", "TESTI_W2"]
    assert svc.scenes_needing_indicators(session, WB, ["TESTI_W1", "TESTI_W2", "TESTI_X"]) == [
        "TESTI_X"
    ]


def test_celery_task_configuration() -> None:
    assert ind_task.queue == "processing"
    assert svc.MaskMissingError in ind_task.autoretry_for
    assert ind_task.max_retries == 4 and ind_task.acks_late is True


# --- S4 acceptance: a real Khadakwasla scene ------------------------------------------


def test_acceptance_khadakwasla_real_scene(session) -> None:  # type: ignore[no-untyped-def]
    """Real Earth Search + MinIO: 4 quality indicators x N zones in the hypertable,
    chips in MinIO, and NDTI inside the plausible inland-water band."""
    from app.core.storage import get_store

    store = get_store()
    d0, d1 = date(2026, 5, 1), date(2026, 5, 10)
    ingest_svc.ingest_water_body(session, store, WB, d0, date_to=d1)
    session.commit()
    mask_svc.process_water_body(session, store, WB, d0, d1)
    session.commit()
    result = svc.process_water_body(session, store, WB, d0, d1)
    session.commit()
    assert result.computed, result

    run = (
        session.execute(
            select(IndicatorRun)
            .where(IndicatorRun.water_body_id == WB, IndicatorRun.status == "done")
            .order_by(IndicatorRun.sensed_at.desc())
        )
        .scalars()
        .first()
    )
    assert run is not None and run.scene_id in result.computed
    zones = session.scalars(select(Zone.id).where(Zone.water_body_id == WB)).all()
    obs = session.scalars(
        select(IndicatorObservation).where(
            IndicatorObservation.scene_id == run.scene_id,
            IndicatorObservation.indicator.in_(QUALITY_INDICATOR_KEYS),
        )
    ).all()
    # Every quality indicator observed on most zones (a dry or cloudy zone may be rejected).
    for key in QUALITY_INDICATOR_KEYS:
        n = sum(1 for o in obs if o.indicator == key)
        assert n >= max(1, len(zones) // 2), (key, n, run.rejected)
    for o in obs:
        assert o.valid_pixel_pct >= 30 and o.n_pixels >= 25
        if o.indicator == "ndti_turbidity":
            assert -0.3 <= o.mean <= 0.5 and -0.3 <= o.p90 <= 0.6, o.mean
        if o.indicator == "sediment_proxy":
            assert 0 <= o.mean <= 0.3
    for key in QUALITY_INDICATOR_KEYS:
        assert store.exists(run.chips[key]["chip_key"])
    assert run.sensed_at >= datetime(2026, 5, 1, tzinfo=UTC)
