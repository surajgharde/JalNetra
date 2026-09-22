"""S9 tests: contract endpoint inventory in OpenAPI, cursors, tile styling, app wiring."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.pagination import decode_cursor, encode_cursor
from app.api.tiles import LAYER_STYLES, layer_of, titiler_params
from app.core.config import Settings
from app.core.health import CHECKS
from app.main import app
from app.services.l02_api.jobs import STAGES, StageCount

CONTRACT_PATHS = {
    "/api/v1/water-bodies": {"get"},
    "/api/v1/water-bodies/{water_body_id}": {"get"},
    "/api/v1/water-bodies/{water_body_id}/observations": {"get"},
    "/api/v1/water-bodies/{water_body_id}/indicators": {"get"},
    "/api/v1/water-bodies/{water_body_id}/series": {"get"},
    "/api/v1/alerts": {"get"},
    "/api/v1/alerts/{alert_id}": {"get"},
    "/api/v1/alerts/{alert_id}/brief.pdf": {"get"},
    "/api/v1/alerts.geojson": {"get"},
    "/api/v1/validations": {"get", "post"},
    "/api/v1/jobs/ingest": {"post"},
    "/api/v1/jobs/{job_id}": {"get"},
    "/api/v1/jobs/{job_id}/report.pdf": {"get"},
    "/api/v1/jobs/{job_id}/report.csv": {"get"},
    "/api/v1/imagery/live": {"get"},
    "/api/v1/imagery/status": {"get"},
    "/tiles/{layer}/{water_body_id}/{on}/{z}/{x}/{y}.png": {"get"},
    "/health": {"get"},
}


def test_openapi_covers_the_frozen_contract_with_examples() -> None:
    spec = app.openapi()
    paths = spec["paths"]
    for path, methods in CONTRACT_PATHS.items():
        assert path in paths, path
        assert methods <= set(paths[path]), path
    # Every JSON response model that the demo shows carries an example for /docs.
    schemas = spec["components"]["schemas"]
    for name in (
        "WaterBodyListItem",
        "ObservationItem",
        "IndicatorsResponse",
        "SeriesResponse",
        "JobOut",
        "ValidationIn",
        "IngestJobRequest",
    ):
        assert schemas[name].get("examples"), name
    assert "disclaimer" in schemas["AlertOut"]["properties"]


def test_cursor_roundtrip_and_validation() -> None:
    c = encode_cursor(1, "Khadakwasla Reservoir", "wb_khadakwasla")
    assert "=" not in c
    assert decode_cursor(c, 3) == [1, "Khadakwasla Reservoir", "wb_khadakwasla"]
    assert decode_cursor(None, 3) is None
    with pytest.raises(HTTPException):
        decode_cursor(c, 2)
    with pytest.raises(HTTPException):
        decode_cursor("not base64!!", 2)


def test_tile_styles_per_layer() -> None:
    s = Settings(minio_bucket="jalnetra")
    p = titiler_params("chips/wb_x/2026-09-17/body/ndti_turbidity.tif", s)
    assert p["url"] == "s3://jalnetra/chips/wb_x/2026-09-17/body/ndti_turbidity.tif"
    assert p["colormap_name"] == "ylorbr" and p["rescale"] == "-0.3,0.5"  # sequential amber
    mask = titiler_params("chips/wb_x/2026-09-17/body/watermask.tif", s)
    assert "colormap_name" not in mask and '"1"' in mask["colormap"] and mask["nodata"] == "255"
    assert LAYER_STYLES["anomaly"].colormap_name == "reds"
    assert layer_of("turbidity") == "ndti_turbidity" and layer_of("watermask") == "watermask"


def test_health_probes_include_stac() -> None:
    assert set(CHECKS) == {"postgres", "redis", "minio", "stac"}


def test_stage_count_and_app_serves_styles() -> None:
    assert StageCount("mask", 3, 4).pct == 75.0 and StageCount("mask", 0, 0).pct == 0.0
    assert STAGES[0] == "ingestion" and STAGES[-1] == "scoring"
    with TestClient(app) as client:
        r = client.get("/tiles/styles")
        assert r.status_code == 200 and r.json()["ndti_turbidity"]["colormap_name"] == "ylorbr"
        assert app.state.limiter is not None  # slowapi wired; default limit from settings
        bad = client.get("/tiles/chip/not-a-chip/10/1/1.png")
        assert bad.status_code == 400
