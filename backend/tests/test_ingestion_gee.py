"""Google Earth Engine source + live imagery: pure helpers and the API surface,
with no Earth Engine session (every server call is stubbed)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest
from affine import Affine
from fastapi import HTTPException
from httpx import AsyncClient

from app.api.v1 import imagery as imagery_api
from app.core.config import Settings, get_settings
from app.core.health import run_health_checks
from app.services.l03_ingestion import gee
from app.services.l03_ingestion.stac import BANDS, ChainedSource, build_source

COLL = "COPERNICUS/S2_SR_HARMONIZED"
INDEX = "20260503T053641_20260503T054650_T43QCA"


def _feature(**overrides: Any) -> dict[str, Any]:
    props = {
        "system:index": INDEX,
        "system:time_start": 1777787801000,  # 2026-05-03T05:56:41Z
        "MGRS_TILE": "43QCA",
        "CLOUDY_PIXEL_PERCENTAGE": 4.2,
        "SPACECRAFT_NAME": "Sentinel-2C",
        **overrides,
    }
    return {
        "type": "Image",
        "id": f"{COLL}/{INDEX}",
        "bands": [
            {
                "id": "B4",
                "crs": "EPSG:32643",
                "crs_transform": [10, 0, 300000, 0, -10, 2100000],
                "dimensions": [10980, 10980],
            }
        ],
        "properties": props,
    }


def test_candidate_from_feature_maps_every_field() -> None:
    cand = gee.candidate_from_feature(_feature(), COLL)
    assert cand is not None
    assert cand.id == INDEX
    assert cand.mgrs_tile == "43QCA"
    assert cand.sensed_at == datetime(2026, 5, 3, 5, 56, 41, tzinfo=UTC)
    assert cand.cloud_pct == 4.2
    assert cand.platform == "sentinel-2c"
    assert cand.source == "gee"
    assert cand.epsg == 32643
    assert cand.boa_add_offset == 0  # harmonized collection
    assert set(cand.assets) == set(BANDS)
    assert cand.assets["B03"] == f"gee://{COLL}/{INDEX}#B3"
    assert gee.split_href(cand.assets["SCL"]) == (f"{COLL}/{INDEX}", "SCL")


def test_candidate_from_feature_falls_back_to_tile_epsg_and_rejects_partial() -> None:
    f = _feature()
    f["bands"] = []
    cand = gee.candidate_from_feature(f, COLL)
    assert cand is not None and cand.epsg == 32643
    assert gee.candidate_from_feature(_feature(MGRS_TILE=None), COLL) is None


@pytest.mark.parametrize(
    ("tile", "epsg"), [("43QCA", 32643), ("44RKN", 32644), ("43PGS", 32643), ("36JTT", 32736)]
)
def test_epsg_of_tile(tile: str, epsg: int) -> None:
    assert gee.epsg_of_tile(tile) == epsg


def test_plan_blocks_tiles_the_grid_exactly() -> None:
    blocks = list(gee.plan_blocks(2500, 1100, 1024))
    assert blocks[0] == (0, 0, 1024, 1024)
    assert blocks[-1] == (2048, 1024, 452, 76)
    covered = np.zeros((1100, 2500), dtype=np.uint8)
    for col, row, w, h in blocks:
        covered[row : row + h, col : col + w] += 1
    assert covered.min() == 1 and covered.max() == 1
    with pytest.raises(ValueError):
        list(gee.plan_blocks(10, 10, 0))


def test_block_px_stays_under_the_response_cap() -> None:
    edge = gee.block_px_for(len(BANDS))
    assert edge * edge * len(BANDS) * 2 < gee.COMPUTE_PIXELS_MAX_BYTES
    assert edge >= 1024  # a 1024 px block (default setting) is safe for six bands


def test_grid_params_describe_the_block_origin() -> None:
    t = Affine(10, 0, 300000, 0, -10, 2100000)
    g = gee.grid_params("EPSG:32643", t, 100, 20, 50, 40)
    assert g["dimensions"] == {"width": 50, "height": 40}
    assert g["affineTransform"]["translateX"] == 301000
    assert g["affineTransform"]["translateY"] == 2099800
    assert g["affineTransform"]["scaleY"] == -10
    assert g["crsCode"] == "EPSG:32643"


def test_raster_bounds_of_band_info() -> None:
    assert gee.raster_bounds_of(_feature()["bands"][0]) == (300000, 1990200, 409800, 2100000)


def test_vis_params_rgb_and_index() -> None:
    rgb = gee.vis_params(gee.LIVE_VIS["truecolor"])
    assert rgb["bands"] == ["B4", "B3", "B2"] and rgb["gamma"] == 1.3
    idx = gee.vis_params(gee.LIVE_VIS["ndti"])
    assert "bands" not in idx and len(idx["palette"]) == 5 and idx["min"] == -0.3
    assert gee.LIVE_VIS["ndti"].water_only and not gee.LIVE_VIS["mndwi"].water_only


def test_live_map_cache_parts_round_the_bbox() -> None:
    a = gee.live_map_cache_parts((73.7001, 18.38, 73.78, 18.45), vis="truecolor", days=30)
    b = gee.live_map_cache_parts((73.7004, 18.38, 73.78, 18.45), vis="truecolor", days=30)
    c = gee.live_map_cache_parts((73.7004, 18.38, 73.78, 18.45), vis="ndti", days=30)
    assert a == b != c


def test_initialize_refuses_when_disabled() -> None:
    with pytest.raises(gee.GEEError):
        gee.initialize(Settings(app_env="test", gee_enabled=False))


# --- initialise retries ----------------------------------------------------------
#
# The OAuth token POST behind ee.Initialize is dropped mid-handshake on a lossy link
# ("[SSL: UNEXPECTED_EOF_WHILE_READING]"), which used to fail startup on the first try.


def _init_settings(**kw: Any) -> Settings:
    return Settings(app_env="test", gee_enabled=True, gee_project="p", gee_high_volume=False, **kw)


@pytest.fixture
def stub_ee(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Neutralise the pieces of ``initialize`` that need a real session."""
    monkeypatch.setattr(gee, "_initialised_for", None, raising=False)
    monkeypatch.setattr(gee, "credentials", lambda settings: None)
    monkeypatch.setattr(gee, "_harden_session", lambda settings: None)
    calls: list[str] = []
    monkeypatch.setattr(gee, "_deadline", lambda ms: calls.append(f"deadline:{ms}"))
    return calls


def _ssl_eof() -> OSError:
    """The exact shape of the reported failure."""
    import ssl

    import requests

    return requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded "
        "with url: /token (Caused by SSLError(ssl.SSLEOFError(8, '[SSL: "
        "UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1016)')))",
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    )


def test_initialize_retries_a_dropped_token_handshake(
    monkeypatch: pytest.MonkeyPatch, stub_ee: list[str]
) -> None:
    attempts = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _ssl_eof()

    monkeypatch.setattr(gee, "_ee_initialize", flaky)
    monkeypatch.setattr(gee, "wait_exponential_jitter", lambda **kw: lambda rs: 0)
    gee.initialize(_init_settings(gee_init_attempts=5), force=True)
    assert attempts["n"] == 3  # two drops survived, third call won


def test_initialize_gives_up_after_the_configured_attempts(
    monkeypatch: pytest.MonkeyPatch, stub_ee: list[str]
) -> None:
    attempts = {"n": 0}

    def always_drops(*args: Any, **kwargs: Any) -> None:
        attempts["n"] += 1
        raise _ssl_eof()

    monkeypatch.setattr(gee, "_ee_initialize", always_drops)
    monkeypatch.setattr(gee, "wait_exponential_jitter", lambda **kw: lambda rs: 0)
    with pytest.raises(gee.GEEError) as err:
        gee.initialize(_init_settings(gee_init_attempts=3), force=True)
    assert attempts["n"] == 3
    assert "oauth2.googleapis.com keeps dropping" in str(err.value)  # actionable hint


def test_initialize_does_not_retry_a_real_rejection(
    monkeypatch: pytest.MonkeyPatch, stub_ee: list[str]
) -> None:
    """A bad project or revoked key must fail fast, not burn five slow attempts."""
    attempts = {"n": 0}

    def rejected(*args: Any, **kwargs: Any) -> None:
        attempts["n"] += 1
        raise ValueError("Caller does not have permission on project 'p'")

    monkeypatch.setattr(gee, "_ee_initialize", rejected)
    with pytest.raises(gee.GEEError) as err:
        gee.initialize(_init_settings(gee_init_attempts=5), force=True)
    assert attempts["n"] == 1
    assert "keeps dropping" not in str(err.value)


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (_ssl_eof(), True),
        (ConnectionResetError("connection reset by peer"), True),
        (TimeoutError("timed out"), True),
        (RuntimeError("503 Service Unavailable"), True),
        (RuntimeError("Not signed up for Earth Engine"), False),
        (ValueError("unknown visualisation"), False),
    ],
)
def test_transient_tells_dropped_links_from_rejections(exc: Exception, transient: bool) -> None:
    assert gee._transient(exc) is transient


def test_build_source_chains_gee_only_when_enabled() -> None:
    plain = build_source(Settings(app_env="test", gee_enabled=False))
    assert isinstance(plain, ChainedSource)
    assert [s.name for s in plain.sources] == ["earth-search", "cdse"]

    with_gee = build_source(Settings(app_env="test", gee_enabled=True, gee_project="p"))
    assert isinstance(with_gee, ChainedSource)
    assert [s.name for s in with_gee.sources] == ["earth-search", "cdse", "gee"]

    primary = build_source(
        Settings(app_env="test", gee_enabled=True, gee_project="p", stac_source="gee")
    )
    assert isinstance(primary, ChainedSource)
    assert [s.name for s in primary.sources] == ["gee", "earth-search", "cdse"]

    with pytest.raises(ValueError):
        build_source(Settings(app_env="test", gee_enabled=False, stac_source="gee"))


def test_parse_bbox_validation() -> None:
    assert imagery_api.parse_bbox("73.7,18.38,73.78,18.45", 8.0) == (73.7, 18.38, 73.78, 18.45)
    for bad in ("1,2,3", "a,b,c,d", "74,18,73,19", "70,10,79,19"):
        with pytest.raises(HTTPException):
            imagery_api.parse_bbox(bad, 8.0)


async def test_live_endpoint_is_503_when_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the flag: a box with GEE configured would otherwise answer 200.
    monkeypatch.setenv("GEE_ENABLED", "false")
    get_settings.cache_clear()
    r = await client.get("/api/v1/imagery/live", params={"bbox": "73.7,18.38,73.78,18.45"})
    assert r.status_code == 503
    s = await client.get("/api/v1/imagery/status")
    assert s.status_code == 200
    body = s.json()
    assert body["enabled"] is False and body["ok"] is False
    assert {v["key"] for v in body["visualisations"]} == set(gee.LIVE_VIS)


async def test_live_endpoint_returns_a_tile_template(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEE_ENABLED", "true")
    monkeypatch.setenv("GEE_PROJECT", "jalnetra-test")
    monkeypatch.setenv("API_CACHE_ENABLED", "false")
    get_settings.cache_clear()  # the client fixture built the app before the env was set
    calls: list[dict[str, Any]] = []

    def fake_live_map(bbox: Any, **kw: Any) -> gee.LiveMap:
        calls.append({"bbox": bbox, **kw})
        return gee.LiveMap(
            vis=kw["vis"],
            mode="composite" if kw["composite"] else "latest",
            tile_url="https://earthengine.googleapis.com/v1/projects/p/maps/m/tiles/{z}/{x}/{y}",
            map_id="projects/p/maps/m",
            scene_date=date(2026, 9, 19),
            scene_count=2,
            cloud_pct=8.4,
            window_from=date(2026, 8, 22),
            window_to=date(2026, 9, 21),
            collection=COLL,
        )

    monkeypatch.setattr(gee, "live_map", fake_live_map)
    r = await client.get(
        "/api/v1/imagery/live",
        params={"bbox": "73.7,18.38,73.78,18.45", "vis": "ndti", "date": "2026-09-21"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tile_url"].endswith("/{z}/{x}/{y}")
    assert body["scene_date"] == "2026-09-19" and body["cached"] is False
    assert calls[0]["vis"] == "ndti" and calls[0]["date_to"] == date(2026, 9, 21)

    r = await client.get(
        "/api/v1/imagery/live", params={"bbox": "73.7,18.38,73.78,18.45", "vis": "x"}
    )
    assert r.status_code == 404
    r = await client.get(
        "/api/v1/imagery/live", params={"bbox": "73.7,18.38,73.78,18.45", "days": 10_000}
    )
    assert r.status_code == 422


async def test_live_endpoint_maps_no_scene_to_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEE_ENABLED", "true")
    monkeypatch.setenv("GEE_PROJECT", "jalnetra-test")
    monkeypatch.setenv("API_CACHE_ENABLED", "false")
    get_settings.cache_clear()  # the client fixture built the app before the env was set

    def no_scene(bbox: Any, **kw: Any) -> gee.LiveMap:
        raise LookupError("no Sentinel-2 pass under 60% cloud in the window")

    monkeypatch.setattr(gee, "live_map", no_scene)
    r = await client.get("/api/v1/imagery/live", params={"bbox": "73.7,18.38,73.78,18.45"})
    assert r.status_code == 404


async def test_health_adds_gee_probe_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(_: Settings) -> None:
        return None

    from app.core import health

    monkeypatch.setattr(health, "CHECKS", {"postgres": ok})
    off = await run_health_checks(Settings(app_env="test", gee_enabled=False))
    assert set(off) == {"postgres"}
    on = await run_health_checks(Settings(app_env="test", gee_enabled=True, gee_project="p"))
    assert set(on) == {"postgres", "gee"}
    assert on["gee"].status == "error"  # no credentials on the test box


# --- the /health probe -----------------------------------------------------------
#
# ping() decides what /health reports for Earth Engine. Its round trip had no retry,
# so one dropped connection on a lossy link marked gee -- and with it the whole API --
# degraded while the session was perfectly usable.


def test_ping_retries_a_dropped_round_trip(
    monkeypatch: pytest.MonkeyPatch, stub_ee: list[str]
) -> None:
    monkeypatch.setattr(gee, "initialize", lambda settings: None)
    monkeypatch.setattr(gee, "wait_exponential_jitter", lambda **kw: lambda rs: 0)
    attempts = {"n": 0}

    def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _ssl_eof()

    monkeypatch.setattr(gee, "_ee_ping", flaky)
    gee.ping(_init_settings(gee_ping_attempts=3))
    assert attempts["n"] == 3


def test_ping_reports_a_link_that_never_comes_back(
    monkeypatch: pytest.MonkeyPatch, stub_ee: list[str]
) -> None:
    monkeypatch.setattr(gee, "initialize", lambda settings: None)
    monkeypatch.setattr(gee, "wait_exponential_jitter", lambda **kw: lambda rs: 0)
    attempts = {"n": 0}

    def always_drops() -> None:
        attempts["n"] += 1
        raise _ssl_eof()

    monkeypatch.setattr(gee, "_ee_ping", always_drops)
    with pytest.raises(gee.GEEError, match="ping failed"):
        gee.ping(_init_settings(gee_ping_attempts=3))
    assert attempts["n"] == 3


def test_ping_does_not_retry_a_real_rejection(
    monkeypatch: pytest.MonkeyPatch, stub_ee: list[str]
) -> None:
    """A revoked key or unregistered project must surface at once, not after N waits."""
    monkeypatch.setattr(gee, "initialize", lambda settings: None)
    attempts = {"n": 0}

    def rejected() -> None:
        attempts["n"] += 1
        raise ValueError("Not signed up for Earth Engine")

    monkeypatch.setattr(gee, "_ee_ping", rejected)
    with pytest.raises(gee.GEEError):
        gee.ping(_init_settings(gee_ping_attempts=5))
    assert attempts["n"] == 1
