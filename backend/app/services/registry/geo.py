"""Small geometry helpers shared by the registry."""

import math
import re
import unicodedata

from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

WGS84 = CRS.from_epsg(4326)


def utm_crs_for(geom: BaseGeometry) -> CRS:
    """UTM zone CRS (WGS84) for the geometry's centroid."""
    lon, lat = geom.centroid.x, geom.centroid.y
    zone = math.floor((lon + 180) / 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def to_utm(geom: BaseGeometry, crs: CRS | None = None) -> tuple[BaseGeometry, CRS]:
    crs = crs or utm_crs_for(geom)
    fwd = Transformer.from_crs(WGS84, crs, always_xy=True).transform
    return transform(fwd, geom), crs


def to_wgs84(geom: BaseGeometry, crs: CRS) -> BaseGeometry:
    inv = Transformer.from_crs(crs, WGS84, always_xy=True).transform
    return transform(inv, geom)


def area_km2(geom: BaseGeometry) -> float:
    """Planar area in the geometry's UTM zone, in km2."""
    projected, _ = to_utm(geom)
    return float(projected.area) / 1e6


def as_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if isinstance(g, Polygon)]
        polys += [p for g in geom.geoms if isinstance(g, MultiPolygon) for p in g.geoms]
        if polys:
            return MultiPolygon(polys)
    raise ValueError(f"cannot coerce {geom.geom_type} to MultiPolygon")


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
