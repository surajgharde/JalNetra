"""Detector 2 - SPATIAL (S6, L8). NumPy + scikit-learn DBSCAN, no I/O.

Finds *where* in the water a deviation sits. Each indicator raster (from the
L6 chip) is turned into a per-pixel robust z against the body's own water
pixels in the same scene:

    z_px = (x_px - median_water) / max(1.4826 * MAD_water, sigma_floor)

This is a within-scene contrast, deliberately independent of the temporal
baseline: a plume is a patch that is unlike the rest of the water *today*, and
that can be seen (and drawn) even while the seasonal baseline is still
building. Pixels with z above the threshold are clustered with DBSCAN over
their row/column coordinates (eps ~3 px, min_samples 10). Each cluster that
covers at least ``min_area_km2`` becomes a real polygon (rasterio shapes ->
shapely, reprojected to WGS84) attributed to the zone holding most of its pixels.

Body-wide shifts are not plumes: if more than ``max_hot_fraction`` of the
water is hot, clustering is skipped and the temporal detector owns the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from affine import Affine
from pyproj import CRS, Transformer
from rasterio import features
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union
from sklearn.cluster import DBSCAN

from app.services.l07_baseline.robust import MAD_TO_SIGMA


@dataclass(frozen=True)
class SpatialCluster:
    indicator: str
    zone_id: str | None  # zone holding most of the cluster's pixels
    n_pixels: int
    area_km2: float
    mean_z: float
    max_z: float
    geom_4326: BaseGeometry  # Polygon or MultiPolygon
    zone_pixel_share: float  # share of the cluster's pixels inside zone_id

    def to_dict(self) -> dict[str, Any]:
        c = self.geom_4326.centroid
        return {
            "indicator": self.indicator,
            "zone_id": self.zone_id,
            "n_pixels": self.n_pixels,
            "area_km2": round(self.area_km2, 4),
            "mean_z": round(self.mean_z, 2),
            "max_z": round(self.max_z, 2),
            "centroid": [round(c.x, 6), round(c.y, 6)],
        }


@dataclass
class SpatialResult:
    clusters: list[SpatialCluster] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # indicator -> why no clustering
    hot_fraction: dict[str, float] = field(default_factory=dict)

    def for_zone(self, zone_id: str) -> list[SpatialCluster]:
        return [c for c in self.clusters if c.zone_id == zone_id]


def pixel_area_km2(transform: Affine) -> float:
    return abs(transform.a * transform.e) / 1e6


def pixel_z(values: np.ndarray, water: np.ndarray, *, sigma_floor: float) -> np.ndarray:
    """Within-scene robust z over water pixels; NaN elsewhere."""
    out = np.full(values.shape, np.nan, dtype=np.float32)
    sel = water & np.isfinite(values)
    if sel.sum() < 2:
        return out
    v = values[sel].astype(np.float64)
    med = float(np.median(v))
    sigma = max(float(MAD_TO_SIGMA * np.median(np.abs(v - med))), sigma_floor)
    out[sel] = ((v - med) / sigma).astype(np.float32)
    return out


def cluster_hot_pixels(
    z: np.ndarray,
    *,
    z_threshold: float,
    eps_px: float,
    min_samples: int,
    max_hot_fraction: float,
    max_hot_pixels: int,
) -> tuple[np.ndarray, str | None, float]:
    """Label raster (0 = background, k >= 1 = cluster) plus a skip reason and the
    hot fraction. One-sided: only *high* pixels form clusters (all four quality
    indicators rise with the condition they proxy)."""
    finite = np.isfinite(z)
    n_water = int(finite.sum())
    labels = np.zeros(z.shape, dtype=np.int32)
    if n_water == 0:
        return labels, "no water pixels", 0.0
    hot = finite & (z > z_threshold)
    n_hot = int(hot.sum())
    frac = n_hot / n_water
    if n_hot < min_samples:
        return labels, None, frac
    if frac > max_hot_fraction:
        return labels, f"{100 * frac:.0f}% of water is hot: body-wide shift, not a plume", frac
    if n_hot > max_hot_pixels:
        return labels, f"{n_hot} hot pixels exceeds the clustering guard", frac
    rows, cols = np.nonzero(hot)
    coords = np.column_stack([rows, cols]).astype(np.float32)
    model = DBSCAN(eps=eps_px, min_samples=min_samples, metric="euclidean").fit(coords)
    lab = model.labels_  # -1 = noise
    labels[rows[lab >= 0], cols[lab >= 0]] = lab[lab >= 0] + 1
    return labels, None, frac


def _polygonize(labels: np.ndarray, k: int, transform: Affine, crs: str) -> BaseGeometry:
    mask = labels == k
    polys = [
        shape(geom)
        for geom, val in features.shapes(mask.astype(np.uint8), mask=mask, transform=transform)
        if val == 1
    ]
    merged = unary_union(polys)
    fwd = Transformer.from_crs(CRS.from_user_input(crs), CRS.from_epsg(4326), always_xy=True)
    out = shp_transform(fwd.transform, merged)
    if isinstance(out, Polygon):
        out = MultiPolygon([out])
    return out


def _majority_zone(mask: np.ndarray, zone_masks: dict[str, np.ndarray]) -> tuple[str | None, float]:
    best, best_n = None, 0
    total = int(mask.sum())
    for zid, zm in zone_masks.items():
        n = int((mask & zm).sum())
        if n > best_n:
            best, best_n = zid, n
    return best, (best_n / total if total else 0.0)


def spatial_detector(
    rasters: dict[str, np.ndarray],
    water: np.ndarray,
    zone_masks: dict[str, np.ndarray],
    transform: Affine,
    crs: str,
    *,
    sigma_floors: dict[str, float],
    z_threshold: float,
    eps_px: float,
    min_samples: int,
    min_area_km2: float,
    max_hot_fraction: float,
    max_hot_pixels: int,
    default_floor: float = 0.01,
) -> SpatialResult:
    """Run the plume finder on every quality-indicator raster of one scene."""
    result = SpatialResult()
    px_km2 = pixel_area_km2(transform)
    for key, values in rasters.items():
        z = pixel_z(values, water, sigma_floor=sigma_floors.get(key, default_floor))
        labels, skip, frac = cluster_hot_pixels(
            z,
            z_threshold=z_threshold,
            eps_px=eps_px,
            min_samples=min_samples,
            max_hot_fraction=max_hot_fraction,
            max_hot_pixels=max_hot_pixels,
        )
        result.hot_fraction[key] = round(frac, 4)
        if skip:
            result.skipped[key] = skip
            continue
        for k in range(1, int(labels.max()) + 1):
            mask = labels == k
            n = int(mask.sum())
            area = n * px_km2
            if area < min_area_km2:
                continue
            zone_id, share = _majority_zone(mask, zone_masks)
            zs = z[mask]
            result.clusters.append(
                SpatialCluster(
                    indicator=key,
                    zone_id=zone_id,
                    n_pixels=n,
                    area_km2=area,
                    mean_z=float(np.nanmean(zs)),
                    max_z=float(np.nanmax(zs)),
                    geom_4326=_polygonize(labels, k, transform, crs),
                    zone_pixel_share=share,
                )
            )
    return result


def union_geom(clusters: list[SpatialCluster]) -> MultiPolygon | None:
    if not clusters:
        return None
    merged = unary_union([c.geom_4326 for c in clusters])
    if isinstance(merged, Polygon):
        return MultiPolygon([merged])
    if isinstance(merged, MultiPolygon):
        return merged
    polys = [g for g in getattr(merged, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(polys) if polys else None
