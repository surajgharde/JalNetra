"""S9 tests: contract endpoint inventory in OpenAPI, cursors, tile styling, app wiring."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.pagination import decode_cursor, encode_cursor
from app.api.tiles import LAYER_STYLES, layer_of, titiler_params
from app.core.config import Settings
from app.core.health import CHECKS
from app.main import app
from app.services.l02_api.jobs import STAGES, StageCount
from app.services.l11_alerts.evidence import tile_url

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
    "/api/v1/methodology": {"get"},
    "/tiles/{layer}/{water_body_id}/{on}/{z}/{x}/{y}.png": {"get"},
    "/tiles/chip/{chip}/{z}/{x}/{y}.png": {"get"},
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


async def test_methodology_reads_from_the_registry(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get("/api/v1/methodology")
    assert r.status_code == 200
    m = r.json()
    assert m["workflow"] == ["Monitoring", "Detection", "Prioritisation", "Investigation support"]
    keys = {i["key"] for i in m["indicators"]}
    assert keys == {
        "ndti_turbidity",
        "ndci_chlorophyll",
        "fai_algal",
        "sediment_proxy",
        "mndwi_extent",
    }
    for ind in m["indicators"]:
        assert ind["formula"] and len(ind["scientific_basis"]) > 80 and ind["observes"]
    assert "Otsu" in " ".join(m["water_detection"]["details"])
    assert "laboratory" in m["product_boundary"] and m["disclaimer"]
    assert any("z_ndti_turbidity" in d for d in m["prioritisation"]["details"])


# --- chip tile route -------------------------------------------------------------
#
# Every alert advertises its evidence overlays as /tiles/chip/<encoded key>/{z}/{x}/{y}.png
# (see l11_alerts.evidence.TILE_TEMPLATE). A chip key always contains slashes and the
# server percent-decodes %2F before routing, so a plain {chip} parameter -- which never
# spans a slash -- matched no real key: every evidence tile 404'd.


def test_chip_route_accepts_a_key_with_slashes() -> None:
    """The route must reach the handler; the handler's own 400 proves it matched."""
    key = quote("chips/wb_demo/2024-12-24/body/sediment_proxy.tif", safe="")
    with TestClient(app) as client:
        r = client.get(f"/tiles/chip/{key}/13/5773/3669.png")
    assert r.status_code != 404, "route did not match a real chip key"


def test_evidence_tile_url_matches_the_chip_route() -> None:
    """The URL the alert payload hands the UI has to resolve to this route."""
    url = tile_url("chips/wb_demo/2024-12-24/body/sediment_proxy.tif")
    assert url is not None
    concrete = url.replace("{z}", "13").replace("{x}", "5773").replace("{y}", "3669")
    with TestClient(app) as client:
        r = client.get(concrete)
    assert r.status_code != 404, f"{concrete} does not resolve"


@pytest.mark.parametrize(
    "key",
    [
        "etc/passwd",
        "chips/../../secret.tif",
        "../../etc/passwd",
        "secrets/key.tif",
    ],
)
def test_chip_route_still_rejects_keys_outside_the_chip_prefixes(key: str) -> None:
    """Widening the parameter must not widen what it will serve."""
    with TestClient(app) as client:
        r = client.get(f"/tiles/chip/{quote(key, safe='')}/13/5773/3669.png")
    assert r.status_code == 400
    assert r.json()["detail"] == "not a chip key"


# --- photo upload route ----------------------------------------------------------
#
# The @router.post decorator sat on the private _read_bounded helper instead of on
# upload_photo, so the registered handler was the helper: its max_bytes argument
# became a REQUIRED query parameter and every upload was rejected 422, while
# upload_photo itself was never routed at all.

PHOTO_PATH = "/api/v1/validations/{validation_id}/photo"


def test_photo_upload_is_routed_to_the_real_handler() -> None:
    op = app.openapi()["paths"][PHOTO_PATH]["post"]
    assert op["operationId"].startswith("upload_photo"), op["operationId"]


def test_photo_upload_takes_only_a_path_parameter_and_a_file() -> None:
    """No internal size cap leaking out as a required query parameter."""
    op = app.openapi()["paths"][PHOTO_PATH]["post"]
    params = {(p["name"], p["in"]) for p in op.get("parameters", [])}
    assert params == {("validation_id", "path")}, params
    assert "max_bytes" not in {name for name, _ in params}
    assert list(op["requestBody"]["content"]) == ["multipart/form-data"]
