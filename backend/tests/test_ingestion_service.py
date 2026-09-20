"""Ingestion service tests. All need Postgres (integration); the acceptance test
additionally hits Earth Search and MinIO for real."""

import time
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import numpy as np
import pytest
from affine import Affine
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.storage import MemoryStore
from app.db.models import Scene, SceneIngestion
from app.services.l03_ingestion import service as svc
from app.services.l03_ingestion.reader import WindowedBands
from app.services.l03_ingestion.stac import BANDS, SceneCandidate, SourceError
from app.workers.tasks import ingest_water_body as ingest_task

pytestmark = pytest.mark.integration

WB = "wb_khadakwasla"


def _cand(scene_id: str, day: date, cloud: float, source: str = "earth-search") -> SceneCandidate:
    return SceneCandidate(
        id=scene_id,
        mgrs_tile="43QCA",
        sensed_at=datetime(day.year, day.month, day.day, 5, 44, tzinfo=UTC),
        cloud_pct=cloud,
        platform="sentinel-2a",
        stac_href=f"https://stac/{scene_id}",
        source=source,
        assets={b: f"https://cogs/{scene_id}/{b}.tif" for b in BANDS},
        epsg=32643,
    )


class _Source:
    name: ClassVar[str] = "fake"

    def __init__(self, cands: list[SceneCandidate], fail: bool = False) -> None:
        self.cands, self.fail, self.calls = cands, fail, 0

    def search(self, *a: Any, **k: Any) -> list[SceneCandidate]:
        self.calls += 1
        if self.fail:
            raise SourceError("fake down")
        return self.cands

    def gdal_env(self) -> dict[str, str]:
        return {}


def _fake_bands(candidate: SceneCandidate, aoi: Any, wb_id: str, **_: Any) -> WindowedBands:
    arrays = {b: np.full((4, 5), 7, dtype=np.uint8 if b == "SCL" else np.uint16) for b in BANDS}
    return WindowedBands(
        scene_id=candidate.id,
        water_body_id=wb_id,
        crs="EPSG:32643",
        transform=Affine(10, 0, 360000, 0, -10, 2040000),
        bounds=(360000, 2039960, 360050, 2040000),
        arrays=arrays,
        bytes_read=sum(a.nbytes for a in arrays.values()),
        duration_s=0.01,
    )


@pytest.fixture
def session():  # type: ignore[no-untyped-def]
    from app.db.sync_session import sync_session

    with sync_session() as s:
        s.execute(text("DELETE FROM scene_ingestions WHERE scene_id LIKE 'TEST_%'"))
        s.execute(text("DELETE FROM scenes WHERE id LIKE 'TEST_%'"))
        s.commit()
        yield s
        s.rollback()
        s.execute(text("DELETE FROM scene_ingestions WHERE scene_id LIKE 'TEST_%'"))
        s.execute(text("DELETE FROM scenes WHERE id LIKE 'TEST_%'"))
        s.commit()


def test_search_persists_scenes_with_usable_flag(session) -> None:  # type: ignore[no-untyped-def]
    src = _Source([_cand("TEST_A", date(2026, 5, 3), 2.2), _cand("TEST_B", date(2026, 5, 8), 80.0)])
    scenes = svc.search_scenes(session, WB, date(2026, 5, 1), date(2026, 5, 10), source=src)
    session.commit()
    assert [s.id for s in scenes] == ["TEST_A", "TEST_B"]
    assert [s.usable for s in scenes] == [True, False]  # 80 % > 60 % threshold
    row = session.get(Scene, "TEST_A")
    assert (
        row is not None and row.assets["B04"].endswith("/B04.tif") and row.source == "earth-search"
    )

    # Re-search from a different source keeps the original provenance.
    svc.search_scenes(
        session,
        WB,
        date(2026, 5, 1),
        date(2026, 5, 10),
        source=_Source([_cand("TEST_A", date(2026, 5, 3), 3.0, source="cdse")]),
    )
    session.commit()
    row = session.get(Scene, "TEST_A")
    assert row is not None and row.source == "earth-search" and row.cloud_pct == 3.0


def test_ingest_is_idempotent(session, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(svc, "read_windowed_bands", _fake_bands)
    store = MemoryStore()
    src = _Source([_cand("TEST_C", date(2026, 5, 3), 5.0), _cand("TEST_D", date(2026, 5, 3), 90.0)])

    first = svc.ingest_water_body(session, store, WB, date(2026, 5, 3), source=src)
    session.commit()
    assert first.ingested == ["TEST_C"] and first.unusable == ["TEST_D"] and first.skipped == []
    key = f"{get_settings().ingest_cache_prefix}/{WB}/TEST_C/bands.npz"
    assert store.exists(key)
    row = session.execute(
        select(SceneIngestion).where(SceneIngestion.scene_id == "TEST_C")
    ).scalar_one()
    assert row.status == "done" and row.cache_key == key and row.bytes_read and row.bytes_read > 0

    second = svc.ingest_water_body(session, store, WB, date(2026, 5, 3), source=src)
    session.commit()
    assert second.skipped == ["TEST_C"] and second.ingested == []  # no-op


def test_failed_read_marks_row_failed_and_raises(session, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    def boom(*a: Any, **k: Any) -> WindowedBands:
        raise OSError("range request failed")

    monkeypatch.setattr(svc, "read_windowed_bands", boom)
    src = _Source([_cand("TEST_E", date(2026, 5, 3), 5.0)])
    with pytest.raises(OSError):
        svc.ingest_water_body(session, MemoryStore(), WB, date(2026, 5, 3), source=src)
    session.commit()
    row = session.execute(
        select(SceneIngestion).where(SceneIngestion.scene_id == "TEST_E")
    ).scalar_one()
    assert row.status == "failed" and row.error and "range request failed" in row.error


def test_pending_scenes_for_tier1(session, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    src = _Source([_cand("TEST_F", date(2026, 5, 3), 5.0)])
    pairs = svc.pending_scenes_for_tier(session, tier=1, lookback_days=7, source=src)
    session.commit()
    tier1 = session.execute(text("SELECT count(*) FROM water_bodies WHERE tier = 1")).scalar_one()
    assert src.calls == tier1  # one search per Tier 1 body
    assert (WB, date(2026, 5, 3)) in pairs


def test_celery_task_retry_configuration() -> None:
    assert ingest_task.autoretry_for and SourceError in ingest_task.autoretry_for
    assert ingest_task.retry_backoff is True and ingest_task.max_retries == 5
    assert ingest_task.retry_backoff_max == 600 and ingest_task.retry_jitter is True


# --- S2 acceptance: one real date for Khadakwasla --------------------------------


def test_acceptance_khadakwasla_real_scene(session) -> None:  # type: ignore[no-untyped-def]
    """Real Earth Search + real MinIO. Under 30 MB, under 60 s, scenes row + cached
    arrays written; rerun is a no-op."""
    from app.core.storage import get_store

    store = get_store()
    day = date(2026, 5, 3)  # S2C_43QCA_20260503_0_L2A, 2.2 % cloud
    session.execute(
        text(
            "DELETE FROM scene_ingestions WHERE water_body_id = :wb AND scene_id LIKE '%20260503%'"
        ),
        {"wb": WB},
    )
    session.commit()

    started = time.perf_counter()
    result = svc.ingest_water_body(session, store, WB, day)
    session.commit()
    elapsed = time.perf_counter() - started

    assert result.scenes_found >= 1 and len(result.ingested) == 1, result
    scene_id = result.ingested[0]
    assert "43QCA" in scene_id and "20260503" in scene_id
    row = session.execute(
        select(SceneIngestion).where(
            SceneIngestion.scene_id == scene_id, SceneIngestion.water_body_id == WB
        )
    ).scalar_one()
    assert row.status == "done" and row.cache_key and store.exists(row.cache_key)
    assert row.bytes_read is not None and row.bytes_read < 30 * 1024 * 1024
    assert elapsed < 60, f"took {elapsed:.1f}s"
    scene = session.get(Scene, scene_id)
    assert scene is not None and scene.usable and scene.source == "earth-search"

    again = svc.ingest_water_body(session, store, WB, day)
    session.commit()
    assert again.skipped == [scene_id] and again.ingested == []
