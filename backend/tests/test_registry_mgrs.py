from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from app.services.registry import mgrs as mgrs_mod
from app.services.registry.mgrs import (
    ComputedGridResolver,
    KmlGridResolver,
    build_cache,
    get_resolver,
    parse_tiling_grid_kml,
)

# Khadakwasla reservoir, Pune (lon, lat).
KHADAKWASLA = box(73.70, 18.38, 73.78, 18.45)


def test_computed_resolver_khadakwasla_is_43qca() -> None:
    tiles = ComputedGridResolver().tiles_for(KHADAKWASLA)
    assert "43QCA" in tiles
    assert all(len(t) == 5 for t in tiles)


def test_computed_resolver_point_in_overlap_strip_gets_west_neighbour() -> None:
    r = ComputedGridResolver()
    # 43QCA's 100 km square starts at easting 300000 in UTM 43N; ~3 km east of that
    # edge lies inside the 9.8 km overlap of the tile to the west (43QBA).
    from pyproj import Transformer

    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)
    lon, lat = inv.transform(303_000, 2_040_000)
    tiles = r.tiles_for_point(lon, lat)
    assert {"43QCA", "43QBA"} <= tiles

    # 30 km inside the square: only the owning tile (and north neighbour if near top).
    lon, lat = inv.transform(330_000, 2_040_000)
    assert r.tiles_for_point(lon, lat) == {"43QCA"}


def test_large_polygon_spanning_two_tiles() -> None:
    # ~110 km wide box across the 43QCA / 43QDA boundary.
    wide = box(73.3, 18.3, 74.4, 18.5)
    tiles = ComputedGridResolver().tiles_for(wide)
    assert {"43QCA", "43QDA"} <= set(tiles)


_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>43QCA</name><MultiGeometry><Polygon><outerBoundaryIs><LinearRing>
<coordinates>73.0,18.0,0 74.1,18.0,0 74.1,19.0,0 73.0,19.0,0 73.0,18.0,0</coordinates>
</LinearRing></outerBoundaryIs></Polygon></MultiGeometry></Placemark>
<Placemark><name>43QDA</name><MultiGeometry><Polygon><outerBoundaryIs><LinearRing>
<coordinates>74.0,18.0,0 75.1,18.0,0 75.1,19.0,0 74.0,19.0,0 74.0,18.0,0</coordinates>
</LinearRing></outerBoundaryIs></Polygon></MultiGeometry></Placemark>
<Placemark><name>44QKE</name><MultiGeometry><Polygon><outerBoundaryIs><LinearRing>
<coordinates>78.0,20.0,0 79.1,20.0,0 79.1,21.0,0 78.0,21.0,0 78.0,20.0,0</coordinates>
</LinearRing></outerBoundaryIs></Polygon></MultiGeometry></Placemark>
</Document></kml>
"""


@pytest.fixture
def kml_file(tmp_path: Path) -> Path:
    p = tmp_path / "grid.kml"
    p.write_text(_KML, encoding="utf-8")
    return p


def test_parse_kml(kml_file: Path) -> None:
    ids, polys = parse_tiling_grid_kml(kml_file)
    assert ids == ["43QCA", "43QDA", "44QKE"]
    assert all(isinstance(p, Polygon) and p.is_valid for p in polys)


def test_kml_resolver_exact_intersections(kml_file: Path, tmp_path: Path) -> None:
    cache = build_cache(kml_file, out=tmp_path / "cache.geojson")
    r = KmlGridResolver.from_geojson(cache)
    assert r.tiles_for(KHADAKWASLA) == ["43QCA"]
    assert r.tiles_for(box(73.9, 18.2, 74.3, 18.4)) == ["43QCA", "43QDA"]
    assert r.tiles_for(box(60.0, 0.0, 61.0, 1.0)) == []


def test_build_cache_bbox_filter(kml_file: Path, tmp_path: Path) -> None:
    cache = build_cache(kml_file, out=tmp_path / "c.geojson", bbox=(72.0, 17.0, 76.0, 19.5))
    r = KmlGridResolver.from_geojson(cache)
    assert r.tiles_for(box(78.5, 20.5, 78.6, 20.6)) == []  # 44QKE filtered out


def test_get_resolver_prefers_cache(
    kml_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mgrs_mod, "cache_path", lambda: tmp_path / "missing.geojson")
    assert isinstance(get_resolver(), ComputedGridResolver)
    cache = build_cache(kml_file, out=tmp_path / "present.geojson")
    monkeypatch.setattr(mgrs_mod, "cache_path", lambda: cache)
    assert isinstance(get_resolver(), KmlGridResolver)
