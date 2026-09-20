"""Split a water body into 4-8 monitoring zones.

Method: rasterise the polygon at ``pixel_m`` in its UTM zone, run k-means over
the pixel centroids, then take the Voronoi cell of each cluster centre clipped
to the polygon. The Voronoi partition is the continuous form of the k-means
assignment (every point goes to its nearest centre), and because Voronoi cells
tile the plane the zones tile the water body with no gaps or overlaps -- their
areas sum to the parent area up to floating point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import MultiPoint, MultiPolygon, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import voronoi_diagram
from sklearn.cluster import KMeans

from app.services.registry.geo import as_multipolygon, to_utm, to_wgs84

MIN_ZONES = 4
MAX_ZONES = 8
# Above this many rasterised pixels the grid step is coarsened to keep k-means fast.
MAX_PIXELS = 60_000

_COMPASS = (
    "Northern",
    "North-eastern",
    "Eastern",
    "South-eastern",
    "Southern",
    "South-western",
    "Western",
    "North-western",
)


@dataclass(frozen=True)
class ZoneSpec:
    seq: int
    name: str
    geom: MultiPolygon  # EPSG:4326
    area_km2: float
    centroid: tuple[float, float]  # lon, lat


def choose_k(area_km2: float) -> int:
    """4 zones up to ~5 km2, one more per further ~6 km2, capped at 8."""
    k = MIN_ZONES + int(max(0.0, area_km2 - 5.0) // 6.0)
    return max(MIN_ZONES, min(MAX_ZONES, k))


def _pixel_centroids(geom_utm: BaseGeometry, pixel_m: float) -> np.ndarray:
    minx, miny, maxx, maxy = geom_utm.bounds
    step = pixel_m
    while True:
        xs = np.arange(minx + step / 2, maxx, step)
        ys = np.arange(miny + step / 2, maxy, step)
        if len(xs) * len(ys) <= MAX_PIXELS or step > 1000:
            break
        step *= 2
    gx, gy = np.meshgrid(xs, ys)
    flat = np.column_stack([gx.ravel(), gy.ravel()])
    inside = shapely.contains_xy(geom_utm, flat[:, 0], flat[:, 1])
    pts = flat[inside]
    if len(pts) < MIN_ZONES:  # tiny polygon: fall back to boundary vertices
        pts = shapely.get_coordinates(geom_utm)
    return np.asarray(pts, dtype=float)


def _compass_name(parent_c: Point, zone_c: Point, scale_m: float) -> str:
    dx, dy = zone_c.x - parent_c.x, zone_c.y - parent_c.y
    if math.hypot(dx, dy) < 0.08 * scale_m:
        return "Central"
    bearing = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    return _COMPASS[int(((bearing + 22.5) % 360.0) // 45.0)]


def generate_zones(
    geom: BaseGeometry, k: int | None = None, pixel_m: float = 20.0, seed: int = 0
) -> list[ZoneSpec]:
    """Partition ``geom`` (EPSG:4326 polygon/multipolygon) into k zones."""
    # Densify long edges (~50 m) so the projected outline follows the stored boundary.
    geom_utm, crs = to_utm(geom.segmentize(0.0005))
    parent_area = geom_utm.area
    k = k or choose_k(parent_area / 1e6)

    pts = _pixel_centroids(geom_utm, pixel_m)
    k = max(1, min(k, len(np.unique(pts, axis=0))))
    if k == 1:
        centres = np.asarray([[geom_utm.centroid.x, geom_utm.centroid.y]])
    else:
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(pts)
        centres = km.cluster_centers_

    if k == 1:
        cells: list[tuple[np.ndarray, BaseGeometry]] = [(centres[0], geom_utm)]
    else:
        envelope = shapely.box(*geom_utm.buffer(1000).bounds)
        vor = voronoi_diagram(MultiPoint(centres.tolist()), envelope=envelope)
        cells = []
        for cell in vor.geoms:
            owner = next((c for c in centres if cell.covers(Point(c))), None)
            if owner is None:
                continue
            clipped = cell.intersection(geom_utm)
            if not clipped.is_empty and clipped.area > 0:
                cells.append((owner, clipped))

    parent_c = geom_utm.centroid
    scale = math.sqrt(parent_area)
    # Deterministic ordering: clockwise from north by bearing, so z1 is stable across runs.
    cells.sort(
        key=lambda oc: (
            (math.degrees(math.atan2(oc[0][0] - parent_c.x, oc[0][1] - parent_c.y)) + 360) % 360
        )
    )

    names_seen: dict[str, int] = {}
    zones: list[ZoneSpec] = []
    for seq, (_owner, clipped) in enumerate(cells, start=1):
        base = _compass_name(parent_c, clipped.centroid, scale)
        names_seen[base] = names_seen.get(base, 0) + 1
        name = f"{base} zone" if names_seen[base] == 1 else f"{base} zone {names_seen[base]}"
        # Densify straight Voronoi edges so they stay tight after reprojection.
        wgs = as_multipolygon(to_wgs84(clipped.segmentize(100.0), crs))
        c = wgs.centroid
        zones.append(
            ZoneSpec(
                seq=seq,
                name=name,
                geom=wgs,
                area_km2=clipped.area / 1e6,
                centroid=(round(c.x, 6), round(c.y, 6)),
            )
        )
    return zones
