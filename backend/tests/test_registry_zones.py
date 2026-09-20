import pytest
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

from app.services.registry.geo import area_km2, as_multipolygon, slugify, utm_crs_for
from app.services.registry.zones import MAX_ZONES, MIN_ZONES, choose_k, generate_zones

# ~8 km x 4 km box near Khadakwasla: ~32 km2.
RESERVOIR = box(73.70, 18.40, 73.776, 18.436)
# Thin L-shaped "river stretch" ~ 6 km long, 150 m wide.
RIVER = unary_union([box(73.85, 18.530, 73.905, 18.5314), box(73.9036, 18.5200, 73.905, 18.5314)])


def test_choose_k_bounds() -> None:
    assert choose_k(0.2) == MIN_ZONES
    assert choose_k(5.0) == MIN_ZONES
    assert choose_k(12.0) == 5
    assert choose_k(500.0) == MAX_ZONES


@pytest.mark.parametrize("geom", [RESERVOIR, RIVER], ids=["reservoir", "river"])
def test_zones_partition_parent(geom: Polygon) -> None:
    zones = generate_zones(geom)
    assert MIN_ZONES <= len(zones) <= MAX_ZONES

    parent = area_km2(geom)
    total = sum(z.area_km2 for z in zones)
    assert abs(total - parent) / parent < 0.01, (total, parent)

    # No overlaps: pairwise intersection area is ~0.
    for i, a in enumerate(zones):
        for b in zones[i + 1 :]:
            assert a.geom.intersection(b.geom).area < 1e-10

    # No gaps: union covers the parent.
    union = unary_union([z.geom for z in zones])
    assert union.symmetric_difference(geom).area / geom.area < 1e-3

    assert [z.seq for z in zones] == list(range(1, len(zones) + 1))
    assert len({z.name for z in zones}) == len(zones)
    assert all(z.name.endswith("zone") or " zone " in z.name for z in zones)
    assert all(isinstance(z.geom, MultiPolygon) for z in zones)


def test_zones_are_deterministic() -> None:
    a = generate_zones(RESERVOIR)
    b = generate_zones(RESERVOIR)
    assert [(z.seq, z.name, round(z.area_km2, 6)) for z in a] == [
        (z.seq, z.name, round(z.area_km2, 6)) for z in b
    ]


def test_explicit_k_respected() -> None:
    assert len(generate_zones(RESERVOIR, k=6)) == 6


def test_tiny_polygon_still_partitions() -> None:
    tiny = box(73.785, 18.535, 73.7865, 18.536)  # ~150 m x 110 m
    zones = generate_zones(tiny)
    assert len(zones) >= 1
    assert abs(sum(z.area_km2 for z in zones) - area_km2(tiny)) / area_km2(tiny) < 0.01


def test_geo_helpers() -> None:
    assert utm_crs_for(RESERVOIR).to_epsg() == 32643
    assert 30 < area_km2(RESERVOIR) < 34
    assert slugify("Mula-Mutha (Pune) stretch") == "mula_mutha_pune_stretch"
    assert isinstance(as_multipolygon(RESERVOIR), MultiPolygon)
