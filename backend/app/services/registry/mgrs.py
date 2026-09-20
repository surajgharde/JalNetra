"""Resolve which Sentinel-2 tiles (100 km MGRS grid squares) cover a geometry.

Two resolvers share one interface:

* ``KmlGridResolver`` -- exact. Uses ESA's official tiling-grid KML, downloaded
  once with ``python -m app.services.registry.mgrs download`` and cached as a
  compact GeoJSON under ``settings.data_dir/mgrs``.
* ``ComputedGridResolver`` -- no download. Derives tile ids arithmetically from
  the MGRS grid. A Sentinel-2 tile is a 109.8 km square anchored at the NW
  corner of its 100 km grid square, so it extends ~9.8 km east and south into
  the neighbouring squares. A point therefore also belongs to the tile to its
  west and/or north whenever it lies in that overlap strip.

``get_resolver()`` picks the KML resolver when the cache exists, else falls back.
"""

from __future__ import annotations

import json
import logging
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import httpx
import mgrs as _mgrs
import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from app.core.config import get_settings
from app.services.registry.geo import WGS84, utm_crs_for

log = logging.getLogger(__name__)

# Sentinel-2 tile overlap beyond its 100 km MGRS square, in metres.
TILE_OVERLAP_M = 9_800
# Spacing of interior sample points for the computed resolver, in metres.
SAMPLE_STEP_M = 5_000
# Maharashtra with margin; keeps the cache to ~60 tiles instead of ~56,000.
MAHARASHTRA_BBOX = (72.0, 15.5, 81.5, 22.5)


class TileResolver(Protocol):
    def tiles_for(self, geom: BaseGeometry) -> list[str]: ...


class ComputedGridResolver:
    """Arithmetic resolver; exact except within a few km of a UTM zone boundary."""

    def __init__(self) -> None:
        self._mgrs = _mgrs.MGRS()

    def tiles_for_point(self, lon: float, lat: float) -> set[str]:
        crs = utm_crs_for(shapely.Point(lon, lat))
        fwd = Transformer.from_crs(WGS84, crs, always_xy=True)
        inv = Transformer.from_crs(crs, WGS84, always_xy=True)
        e, n = fwd.transform(lon, lat)
        shifts = (
            (0, 0),
            (-TILE_OVERLAP_M, 0),
            (0, TILE_OVERLAP_M),
            (-TILE_OVERLAP_M, TILE_OVERLAP_M),
        )
        tiles: set[str] = set()
        for de, dn in shifts:
            plon, plat = inv.transform(e + de, n + dn)
            tiles.add(str(self._mgrs.toMGRS(plat, plon, MGRSPrecision=0)))
        return tiles

    def tiles_for(self, geom: BaseGeometry) -> list[str]:
        tiles: set[str] = set()
        for lon, lat in _sample_points(geom):
            tiles |= self.tiles_for_point(lon, lat)
        return sorted(tiles)


class KmlGridResolver:
    """Exact resolver backed by the ESA tiling grid (cached GeoJSON)."""

    def __init__(self, tile_ids: list[str], polygons: list[BaseGeometry]) -> None:
        if len(tile_ids) != len(polygons):
            raise ValueError("tile_ids and polygons length mismatch")
        self._ids = tile_ids
        self._tree = STRtree(polygons)

    @classmethod
    def from_geojson(cls, path: Path) -> KmlGridResolver:
        fc = json.loads(path.read_text(encoding="utf-8"))
        ids = [f["properties"]["tile"] for f in fc["features"]]
        polys = [shape(f["geometry"]) for f in fc["features"]]
        return cls(ids, polys)

    def tiles_for(self, geom: BaseGeometry) -> list[str]:
        hits = self._tree.query(geom, predicate="intersects")
        return sorted({self._ids[int(i)] for i in hits})


def _sample_points(geom: BaseGeometry) -> Iterable[tuple[float, float]]:
    """Boundary vertices plus an interior grid every SAMPLE_STEP_M, in lon/lat."""
    crs = utm_crs_for(geom)
    fwd = Transformer.from_crs(WGS84, crs, always_xy=True)
    inv = Transformer.from_crs(crs, WGS84, always_xy=True)
    projected = shapely.transform(
        geom, lambda xy: np.column_stack(fwd.transform(xy[:, 0], xy[:, 1]))
    )

    pts: list[tuple[float, float]] = [(geom.centroid.x, geom.centroid.y)]
    for x, y in shapely.get_coordinates(geom.simplify(0.001)):
        pts.append((float(x), float(y)))

    minx, miny, maxx, maxy = projected.bounds
    xs = np.arange(minx, maxx + SAMPLE_STEP_M, SAMPLE_STEP_M)
    ys = np.arange(miny, maxy + SAMPLE_STEP_M, SAMPLE_STEP_M)
    gx, gy = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(projected, gx.ravel(), gy.ravel())
    if inside.any():
        lons, lats = inv.transform(gx.ravel()[inside], gy.ravel()[inside])
        pts.extend(zip(map(float, lons), map(float, lats), strict=True))
    return pts


# --- KML download and parsing -------------------------------------------------

_KML_NS = "{http://www.opengis.net/kml/2.2}"


def parse_tiling_grid_kml(kml_path: Path) -> tuple[list[str], list[Polygon]]:
    """Stream-parse ESA's tiling grid KML into (tile id, outer polygon) lists."""
    ids: list[str] = []
    polys: list[Polygon] = []
    for _event, elem in ET.iterparse(kml_path, events=("end",)):
        if elem.tag != f"{_KML_NS}Placemark":
            continue
        name = elem.findtext(f"{_KML_NS}name")
        coords = elem.find(f".//{_KML_NS}Polygon//{_KML_NS}coordinates")
        if name and coords is not None and coords.text:
            ring = [(float(c.split(",")[0]), float(c.split(",")[1])) for c in coords.text.split()]
            if len(ring) >= 4:
                ids.append(name.strip())
                polys.append(Polygon(ring))
        elem.clear()
    return ids, polys


def cache_path() -> Path:
    return get_settings().data_dir / "mgrs" / "s2_tiles.geojson"


def build_cache(
    kml_path: Path,
    out: Path | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> Path:
    """Convert the KML to a compact GeoJSON, optionally clipped to a lon/lat bbox."""
    out = out or cache_path()
    ids, polys = parse_tiling_grid_kml(kml_path)
    window = shapely.box(*bbox) if bbox else None
    feats = [
        {
            "type": "Feature",
            "properties": {"tile": tile_id},
            "geometry": json.loads(shapely.to_geojson(poly)),
        }
        for tile_id, poly in zip(ids, polys, strict=True)
        if window is None or poly.intersects(window)
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    log.info("mgrs grid cache written", extra={"path": str(out), "tiles": len(feats)})
    return out


def download_kml(dest: Path, url: str | None = None) -> Path:
    url = url or get_settings().s2_grid_kml_url
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r, dest.open("wb") as fh:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            fh.write(chunk)
    return dest


def get_resolver() -> TileResolver:
    path = cache_path()
    if path.exists():
        return KmlGridResolver.from_geojson(path)
    return ComputedGridResolver()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "download":
        print("usage: python -m app.services.registry.mgrs download [--all]", file=sys.stderr)
        return 2
    kml = get_settings().data_dir / "mgrs" / "s2_tiling_grid.kml"
    if not kml.exists():
        log.info("downloading tiling grid", extra={"url": get_settings().s2_grid_kml_url})
        download_kml(kml)
    bbox = None if "--all" in argv else MAHARASHTRA_BBOX
    print(build_cache(kml, bbox=bbox))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
