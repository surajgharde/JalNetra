"""Fetch ``natural=water`` polygons from OpenStreetMap via the Overpass API and
return them as a GeoJSON FeatureCollection the loader can ingest."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import linemerge, polygonize, unary_union

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass answers 406 to requests without an explicit Accept / User-Agent.
HEADERS = {"Accept": "application/json", "User-Agent": "JalNetra/0.1 (water-quality research)"}

log = logging.getLogger(__name__)

Bbox = tuple[float, float, float, float]  # south, west, north, east (Overpass order)


def build_query(bbox: Bbox, name_regex: str | None = None, min_area_hint: bool = True) -> str:
    s, w, n, e = bbox
    name_filter = f'["name"~"{name_regex}",i]' if name_regex else '["name"]'
    return (
        "[out:json][timeout:180];\n"
        "(\n"
        f'  way["natural"="water"]{name_filter}({s},{w},{n},{e});\n'
        f'  relation["natural"="water"]{name_filter}({s},{w},{n},{e});\n'
        ");\n"
        "out geom;\n"
    )


def fetch(query: str, url: str = OVERPASS_URL, timeout: float = 240.0) -> dict[str, Any]:
    r = httpx.post(url, data={"data": query}, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    payload: dict[str, Any] = r.json()
    return payload


def _way_ring(way: dict[str, Any]) -> list[tuple[float, float]]:
    return [(float(p["lon"]), float(p["lat"])) for p in way.get("geometry", [])]


def _assemble_relation(rel: dict[str, Any]) -> Polygon | MultiPolygon | None:
    outers: list[LineString] = []
    inners: list[LineString] = []
    for m in rel.get("members", []):
        if m.get("type") != "way" or "geometry" not in m:
            continue
        coords = [(float(p["lon"]), float(p["lat"])) for p in m["geometry"]]
        if len(coords) < 2:
            continue
        (inners if m.get("role") == "inner" else outers).append(LineString(coords))
    if not outers:
        return None
    outer_polys = list(polygonize(unary_union(linemerge(outers))))
    if not outer_polys:
        return None
    shell = unary_union(outer_polys)
    if inners:
        holes = unary_union(list(polygonize(unary_union(linemerge(inners)))))
        shell = shell.difference(holes)
    if shell.is_empty:
        return None
    return shell if isinstance(shell, Polygon | MultiPolygon) else None


def to_feature_collection(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert an Overpass ``out geom`` payload to GeoJSON polygons."""
    features: list[dict[str, Any]] = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {})
        geom: Polygon | MultiPolygon | None = None
        if el.get("type") == "way":
            ring = _way_ring(el)
            if len(ring) >= 4 and ring[0] == ring[-1]:
                geom = Polygon(ring)
        elif el.get("type") == "relation":
            geom = _assemble_relation(el)
        if geom is None or geom.is_empty:
            continue
        geom = shapely.make_valid(geom)
        features.append(
            {
                "type": "Feature",
                "id": f"{el['type']}/{el['id']}",
                "properties": {
                    "name": tags.get("name"),
                    "name_en": tags.get("name:en"),
                    "water": tags.get("water"),
                    "osm_id": f"{el['type']}/{el['id']}",
                },
                "geometry": json.loads(shapely.to_geojson(geom)),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def fetch_water_polygons(bbox: Bbox, name_regex: str | None = None) -> dict[str, Any]:
    payload = fetch(build_query(bbox, name_regex))
    fc = to_feature_collection(payload)
    log.info("overpass fetched", extra={"features": len(fc["features"])})
    return fc
