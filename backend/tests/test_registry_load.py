"""Loader, seed-file and Overpass tests. DB-backed tests are marked integration."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box, shape
from sqlalchemy import func, select, text

from app.services.registry.geo import area_km2
from app.services.registry.load import assign_tier, build_records, upsert_records
from app.services.registry.mgrs import ComputedGridResolver
from app.services.registry.overpass import build_query, to_feature_collection
from app.services.registry.seed import SEED_FILE
from app.services.registry.zones import generate_zones

# --- tiering ------------------------------------------------------------------


def test_assign_tier_rules() -> None:
    assert assign_tier(25.0) == 1
    assert assign_tier(3.0) == 2
    assert assign_tier(0.2) == 3
    assert assign_tier(0.2, drinking_water=True) == 1
    assert assign_tier(0.2, kind="river_stretch", urban=True) == 1
    assert assign_tier(0.2, kind="reservoir", urban=True) == 3


# --- build_records ------------------------------------------------------------


def test_build_records_from_geodataframe() -> None:
    gdf = gpd.GeoDataFrame(
        {
            "name": ["Test Lake", "Big Reservoir", None],
            "tier": [None, 1, None],
            "drinking_water": ["yes", None, None],
        },
        geometry=[
            box(73.70, 18.40, 73.71, 18.41),
            box(73.60, 18.30, 73.70, 18.40),
            box(73.50, 18.20, 73.51, 18.21),
        ],
        crs="EPSG:4326",
    )
    recs = build_records(gdf, default_district="Pune", resolver=ComputedGridResolver())
    assert [r.id for r in recs] == ["wb_test_lake", "wb_big_reservoir"]  # unnamed skipped
    lake, big = recs
    assert lake.tier == 1 and lake.district == "Pune"  # drinking water overrides area
    assert big.tier == 1 and big.area_km2 > 100
    assert "43QCA" in lake.mgrs_tiles
    assert lake.geom.geom_type == "MultiPolygon"


def test_build_records_requires_district() -> None:
    gdf = gpd.GeoDataFrame({"name": ["x"]}, geometry=[box(73.7, 18.4, 73.71, 18.41)], crs=4326)
    with pytest.raises(ValueError, match="district"):
        build_records(gdf, resolver=ComputedGridResolver())


def test_build_records_reprojects_to_wgs84() -> None:
    gdf = gpd.GeoDataFrame(
        {"name": ["utm"]}, geometry=[box(373_000, 2_038_000, 374_000, 2_039_000)], crs="EPSG:32643"
    )
    (rec,) = build_records(gdf, default_district="Pune", resolver=ComputedGridResolver())
    minx, miny, maxx, maxy = rec.geom.bounds
    assert 73 < minx < maxx < 75 and 18 < miny < maxy < 19
    assert abs(rec.area_km2 - 1.0) < 0.01


# --- seed file: the S1 acceptance criteria, without a database ---------------


@pytest.fixture(scope="module")
def seed_fc() -> dict:  # type: ignore[type-arg]
    return json.loads(Path(SEED_FILE).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_seed_has_at_least_25_valid_pune_bodies(seed_fc: dict) -> None:  # type: ignore[type-arg]
    feats = seed_fc["features"]
    assert len(feats) >= 25
    ids = [f["properties"]["id"] for f in feats]
    assert len(set(ids)) == len(ids)
    for f in feats:
        g = shape(f["geometry"])
        assert g.is_valid and not g.is_empty, f["properties"]["id"]
        assert f["properties"]["district"] == "Pune"
        assert f["properties"]["tier"] in (1, 2, 3)
        lon, lat = g.centroid.x, g.centroid.y
        assert 73.3 < lon < 75.3 and 17.9 < lat < 19.5, f["properties"]["id"]


def test_seed_contains_named_bodies(seed_fc: dict) -> None:  # type: ignore[type-arg]
    ids = {f["properties"]["id"] for f in seed_fc["features"]}
    assert {
        "wb_khadakwasla",
        "wb_panshet",
        "wb_varasgaon",
        "wb_pashan_lake",
        "wb_mula_mutha",
    } <= ids


def test_khadakwasla_is_where_the_plan_says(seed_fc: dict) -> None:  # type: ignore[type-arg]
    kw = next(f for f in seed_fc["features"] if f["properties"]["id"] == "wb_khadakwasla")
    g = shape(kw["geometry"])
    # The dam wall / eastern end is at ~ [73.7712, 18.4419] (plan's zone centroid example).
    assert g.distance(shape({"type": "Point", "coordinates": [73.7712, 18.4419]})) < 0.02
    assert 5 < area_km2(g) < 20


def test_every_seed_body_resolves_to_a_tile(seed_fc: dict) -> None:  # type: ignore[type-arg]
    r = ComputedGridResolver()
    for f in seed_fc["features"]:
        tiles = r.tiles_for(shape(f["geometry"]))
        assert tiles, f["properties"]["id"]
        assert all(t[:2] in ("43", "44") for t in tiles)


def test_seed_zone_areas_sum_within_1pct(seed_fc: dict) -> None:  # type: ignore[type-arg]
    for f in seed_fc["features"]:
        g = shape(f["geometry"])
        zones = generate_zones(g)
        parent = area_km2(g)
        assert abs(sum(z.area_km2 for z in zones) - parent) / parent < 0.01, f["properties"]["id"]
        assert 1 <= len(zones) <= 8


# --- overpass -----------------------------------------------------------------


def test_overpass_query_shape() -> None:
    q = build_query((18.0, 73.0, 19.0, 74.0), name_regex="Khadakwasla")
    assert 'way["natural"="water"]["name"~"Khadakwasla",i](18.0,73.0,19.0,74.0)' in q
    assert "out geom" in q


def test_overpass_assembles_ways_and_relations() -> None:
    sq = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "tags": {"name": "Way Lake", "water": "lake"},
                "geometry": [{"lon": x, "lat": y} for x, y in sq],
            },
            {
                "type": "relation",
                "id": 2,
                "tags": {"name": "Rel Reservoir"},
                "members": [
                    {
                        "type": "way",
                        "role": "outer",
                        "geometry": [{"lon": x, "lat": y} for x, y in [(2, 0), (4, 0), (4, 2)]],
                    },
                    {
                        "type": "way",
                        "role": "outer",
                        "geometry": [{"lon": x, "lat": y} for x, y in [(4, 2), (2, 2), (2, 0)]],
                    },
                    {
                        "type": "way",
                        "role": "inner",
                        "geometry": [
                            {"lon": x, "lat": y}
                            for x, y in [(2.5, 0.5), (3.5, 0.5), (3.5, 1.5), (2.5, 1.5), (2.5, 0.5)]
                        ],
                    },
                ],
            },
            {
                "type": "way",
                "id": 3,
                "tags": {"name": "open line"},
                "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 1}],
            },
        ]
    }
    fc = to_feature_collection(payload)
    assert [f["properties"]["name"] for f in fc["features"]] == ["Way Lake", "Rel Reservoir"]
    rel = shape(fc["features"][1]["geometry"])
    assert abs(rel.area - (4.0 - 1.0)) < 1e-9  # outer 2x2 minus 1x1 hole


# --- database-backed ----------------------------------------------------------


@pytest.mark.integration
def test_upsert_is_idempotent_and_regenerates_zones_only_on_geometry_change() -> None:
    from app.db.models import WaterBody, Zone
    from app.db.sync_session import sync_session

    def records(east: float) -> list:  # type: ignore[type-arg]
        gdf = gpd.GeoDataFrame(
            {"name": ["Upsert Test Lake"], "id": ["wb_upsert_test"]},
            geometry=[box(73.70, 18.40, east, 18.42)],
            crs=4326,
        )
        return build_records(gdf, default_district="Pune", resolver=ComputedGridResolver())

    with sync_session() as s:
        s.execute(text("DELETE FROM water_bodies WHERE id = 'wb_upsert_test'"))
        s.commit()
        try:
            first = upsert_records(s, records(73.72))
            s.commit()
            zone_ids = sorted(
                s.scalars(select(Zone.id).where(Zone.water_body_id == "wb_upsert_test")).all()
            )
            assert first.inserted == 1 and first.zones_built == len(zone_ids) >= 4

            second = upsert_records(s, records(73.72))  # same geometry: no zone churn
            s.commit()
            assert second.updated == 1 and second.zones_built == 0

            third = upsert_records(s, records(73.73))  # geometry changed: zones rebuilt
            s.commit()
            assert third.updated == 1 and third.zones_built >= 4

            # Zone areas persisted in PostGIS sum to the (new) parent within 1%.
            parent = s.scalar(select(WaterBody.area_km2).where(WaterBody.id == "wb_upsert_test"))
            total = s.scalar(
                select(func.sum(Zone.area_km2)).where(Zone.water_body_id == "wb_upsert_test")
            )
            assert parent and total and abs(total - parent) / parent < 0.01
        finally:
            s.rollback()
            s.execute(text("DELETE FROM water_bodies WHERE id = 'wb_upsert_test'"))
            s.commit()


@pytest.mark.integration
def test_seeded_registry_meets_acceptance() -> None:
    from app.db.models import WaterBody, Zone
    from app.db.sync_session import sync_session
    from app.services.registry.seed import seed_pune_water_bodies

    seed_pune_water_bodies()
    with sync_session() as s:
        n = s.scalar(
            select(func.count()).select_from(WaterBody).where(WaterBody.district == "Pune")
        )
        assert n is not None and n >= 25
        invalid = s.scalar(
            select(func.count()).select_from(WaterBody).where(~func.ST_IsValid(WaterBody.geom))
        )
        assert invalid == 0
        no_tiles = s.scalar(
            select(func.count())
            .select_from(WaterBody)
            .where(func.cardinality(WaterBody.mgrs_tiles) == 0)
        )
        assert no_tiles == 0
        rows = s.execute(
            select(WaterBody.id, WaterBody.area_km2, func.sum(Zone.area_km2))
            .join(Zone, Zone.water_body_id == WaterBody.id)
            .group_by(WaterBody.id, WaterBody.area_km2)
        ).all()
        assert len(rows) >= 25
        for wb_id, parent, total in rows:
            assert abs(total - parent) / parent < 0.01, wb_id
        # GIST indexes exist.
        idx = (
            s.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE indexname IN ('idx_water_bodies_geom','idx_zones_geom')"
                )
            )
            .scalars()
            .all()
        )
        assert set(idx) == {"idx_water_bodies_geom", "idx_zones_geom"}
