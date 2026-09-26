"""Geographic search & discovery (S14).

Pure helpers and ``to_water_body_record`` run anywhere. ``geocode_place`` and
``scan_water_bodies`` are tested against a faked httpx transport (never the
real Nominatim/Overpass), matching how ``test_ingestion_stac.py`` fakes STAC.
``annotate_overlaps`` and ``import_discovered`` need Postgres and a seeded
registry, so they are integration-marked like the rest of l02_api.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from shapely.geometry import Polygon, mapping
from sqlalchemy import text

from app.core.config import Settings
from app.main import app
from app.services.l02_api import discovery as l02_disc
from app.services.registry.discovery import (
    DiscoveryError,
    GeocodeResult,
    PlaceNotFoundError,
    _build_radius_query,
    _kind_of,
    geocode_place,
    scan_water_bodies,
    to_water_body_record,
)
from app.services.registry.load import assign_tier

WB = "wb_khadakwasla"

# A small square near Khadakwasla, far enough from the real reservoir shape
# that it does not overlap it, for tests that need "some polygon".
SQUARE = Polygon([(73.90, 18.60), (73.91, 18.60), (73.91, 18.61), (73.90, 18.61)])


# --- pure helpers ------------------------------------------------------------------


def test_kind_of_maps_the_osm_water_tag() -> None:
    assert _kind_of("reservoir") == "reservoir"
    assert _kind_of("basin") == "reservoir"
    assert _kind_of("river") == "river_stretch"
    assert _kind_of("lake") == "lake"
    assert _kind_of("pond") == "lake"
    assert _kind_of(None) == "lake"  # untagged natural=water is still a lake by default


def test_build_radius_query_is_a_circle_not_a_bbox() -> None:
    q = _build_radius_query(21.15, 79.09, 15.0)
    assert "around:15000,21.15,79.09" in q
    assert 'natural"="water"' in q
    assert 'water"="reservoir"' in q


def test_to_water_body_record_computes_area_tier_and_id() -> None:
    record = to_water_body_record(mapping(SQUARE), name="Test Pond", district="Pune", kind="lake")
    assert record.id == "wb_test_pond"
    assert record.district == "Pune"
    assert record.kind == "lake"
    assert record.area_km2 > 0
    assert record.tier == assign_tier(record.area_km2, kind="lake")
    assert record.mgrs_tiles  # resolved from the real MGRS grid


def test_to_water_body_record_honours_explicit_overrides() -> None:
    record = to_water_body_record(
        mapping(SQUARE),
        name="Test Pond",
        district="Pune",
        kind="lake",
        water_body_id="wb_custom_id",
        tier=1,
        source="manual",
    )
    assert record.id == "wb_custom_id"
    assert record.tier == 1
    assert record.source == "manual"


def test_to_water_body_record_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        to_water_body_record(mapping(SQUARE), name="X", district="Pune", kind="swamp")


def test_to_water_body_record_rejects_empty_geometry() -> None:
    empty = {"type": "Polygon", "coordinates": []}
    with pytest.raises(ValueError, match="empty"):
        to_water_body_record(empty, name="X", district="Pune", kind="lake")


# --- geocode_place / scan_water_bodies (faked transport) --------------------------


class _FakeAsyncClient:
    """Minimal async context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, responder: Any, **kw: Any) -> None:
        self._responder = responder

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(
        self, url: str, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        return self._responder("GET", url, params)

    async def post(
        self, url: str, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return self._responder("POST", url, data)


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, responder: Any) -> None:
    monkeypatch.setattr(
        "app.services.registry.discovery.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(responder, **kw),
    )


async def test_geocode_place_parses_the_best_district_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def responder(method: str, url: str, params: Any) -> httpx.Response:
        assert method == "GET"
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "21.1498134",
                    "lon": "79.0820556",
                    "display_name": "Nagpur, Maharashtra, India",
                    "address": {"state_district": "Nagpur", "state": "Maharashtra"},
                }
            ],
            request=httpx.Request("GET", url),
        )

    _install_fake_client(monkeypatch, responder)
    result = await geocode_place("Nagpur", Settings(app_env="test"))
    assert result == GeocodeResult(
        lat=21.1498134,
        lon=79.0820556,
        display_name="Nagpur, Maharashtra, India",
        district="Nagpur",
        state="Maharashtra",
    )


async def test_geocode_place_falls_back_through_address_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nominatim's district-equivalent field varies; county/city/town are tried
    in order when state_district is absent."""

    def responder(method: str, url: str, params: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "18.5",
                    "lon": "73.8",
                    "display_name": "Somewhere",
                    "address": {"county": "Pune"},
                }
            ],
            request=httpx.Request("GET", url),
        )

    _install_fake_client(monkeypatch, responder)
    result = await geocode_place("Somewhere", Settings(app_env="test"))
    assert result.district == "Pune"


async def test_geocode_place_raises_not_found_on_an_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def responder(method: str, url: str, params: Any) -> httpx.Response:
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    _install_fake_client(monkeypatch, responder)
    with pytest.raises(PlaceNotFoundError):
        await geocode_place("Nowhere At All", Settings(app_env="test"))


async def test_geocode_place_wraps_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(method: str, url: str, params: Any) -> httpx.Response:
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    _install_fake_client(monkeypatch, responder)
    with pytest.raises(DiscoveryError):
        await geocode_place("Nagpur", Settings(app_env="test"))


def _overpass_way(
    osm_id: int, name: str, water_tag: str, ring: list[tuple[float, float]]
) -> dict[str, Any]:
    return {
        "type": "way",
        "id": osm_id,
        "tags": {"name": name, "water": water_tag},
        "geometry": [{"lat": lat, "lon": lon} for lon, lat in ring],
    }


async def test_scan_water_bodies_maps_and_sorts_by_area(monkeypatch: pytest.MonkeyPatch) -> None:
    big_ring = [(79.00, 21.00), (79.02, 21.00), (79.02, 21.02), (79.00, 21.02), (79.00, 21.00)]
    small_ring = [
        (79.10, 21.10),
        (79.101, 21.10),
        (79.101, 21.101),
        (79.10, 21.101),
        (79.10, 21.10),
    ]
    payload = {
        "elements": [
            _overpass_way(1, "Small Pond", "pond", small_ring),
            _overpass_way(2, "Big Reservoir", "reservoir", big_ring),
        ]
    }

    def responder(method: str, url: str, data: Any) -> httpx.Response:
        assert method == "POST"
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    _install_fake_client(monkeypatch, responder)
    found = await scan_water_bodies(21.01, 79.01, 15.0, Settings(app_env="test"))
    assert [f.name for f in found] == ["Big Reservoir", "Small Pond"]
    assert found[0].kind == "reservoir"
    assert found[1].kind == "lake"
    assert all(f.distance_km >= 0 for f in found)
    assert all(f.suggested_tier in (1, 2, 3) for f in found)


async def test_scan_water_bodies_truncates_at_the_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _ring(i: int) -> list[tuple[float, float]]:
        x0, x1, y1 = 79.0 + i * 0.01, 79.001 + i * 0.01, 21.001
        return [(x0, 21.0), (x1, 21.0), (x1, y1), (x0, y1), (x0, 21.0)]

    ways = [_overpass_way(i, f"Pond {i}", "pond", _ring(i)) for i in range(10)]

    def responder(method: str, url: str, data: Any) -> httpx.Response:
        return httpx.Response(200, json={"elements": ways}, request=httpx.Request("POST", url))

    _install_fake_client(monkeypatch, responder)
    found = await scan_water_bodies(
        21.0, 79.0, 15.0, Settings(app_env="test", discovery_max_results=3)
    )
    assert len(found) == 3


async def test_scan_water_bodies_wraps_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(method: str, url: str, data: Any) -> httpx.Response:
        raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    _install_fake_client(monkeypatch, responder)
    with pytest.raises(DiscoveryError):
        await scan_water_bodies(21.0, 79.0, 15.0, Settings(app_env="test"))


# --- contract (no database) --------------------------------------------------------


def test_openapi_exposes_search_and_import() -> None:
    paths = app.openapi()["paths"]
    assert "post" in paths["/api/v1/water-bodies/search-and-discover"]
    assert "post" in paths["/api/v1/water-bodies/import-dynamic"]


# --- behaviour (Postgres + seeded registry) ----------------------------------------


@pytest.fixture
def clean() -> Any:
    from app.db.sync_session import sync_session

    def _wipe(s: Any) -> None:
        s.execute(text("DELETE FROM water_bodies WHERE id LIKE 'wb_discovery_test_%'"))
        s.commit()

    with sync_session() as s:
        _wipe(s)
        yield s
        _wipe(s)


@pytest.mark.integration
def test_annotate_overlaps_flags_a_polygon_over_an_existing_water_body(clean: Any) -> None:
    from geoalchemy2.shape import to_shape

    from app.db.models import WaterBody

    existing = to_shape(clean.get(WaterBody, WB).geom)
    # A candidate equal to the existing water body overlaps it 100%.
    candidates = [("way/dup", mapping(existing)), ("way/far", mapping(SQUARE))]
    overlaps = l02_disc.annotate_overlaps(clean, candidates)
    assert overlaps == {"way/dup": WB}
    assert "way/far" not in overlaps


@pytest.mark.integration
def test_import_discovered_creates_then_updates(clean: Any) -> None:
    from sqlalchemy import select

    from app.db.models import WaterBody, Zone

    geometry = json.loads(json.dumps(mapping(SQUARE)))
    result = l02_disc.import_discovered(
        clean,
        geometry,
        name="Discovery Test Pond",
        district="Pune",
        kind="lake",
        water_body_id="wb_discovery_test_pond",
    )
    clean.commit()
    assert result["created"] is True
    assert result["n_zones"] >= 4
    wb = clean.get(WaterBody, "wb_discovery_test_pond")
    assert wb is not None and wb.name == "Discovery Test Pond"
    n_zones = clean.execute(
        select(Zone.id).where(Zone.water_body_id == "wb_discovery_test_pond")
    ).all()
    assert len(n_zones) == result["n_zones"]

    again = l02_disc.import_discovered(
        clean,
        geometry,
        name="Discovery Test Pond Renamed",
        district="Pune",
        kind="lake",
        water_body_id="wb_discovery_test_pond",
    )
    clean.commit()
    assert again["created"] is False
    # upsert_records writes through Core (bulk-friendly), which does not sync an
    # already-loaded ORM instance still sitting in the identity map from the
    # first `.get()` above -- expire it so the next access re-reads the row.
    clean.expire_all()
    wb2 = clean.get(WaterBody, "wb_discovery_test_pond")
    assert wb2.name == "Discovery Test Pond Renamed"
