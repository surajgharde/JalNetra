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

``zone_localised_clusters`` is a *second* pass for the zones this one leaves
empty. The body-wide contrast above is dominated by whichever zone is worst, so
a zone whose own seasonal mean deviated can easily hold no body-level cluster --
43% of temporal-only candidates here sat in a scene where the plume landed in a
different zone. That pass re-runs the same contrast with the zone as its own
reference population, which answers "where *inside this zone*" and gives the map
a polygon to draw instead of the whole zone outline. It is localisation only: it
never votes, so severity and scoring are untouched.
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
    """Within-scene robust z over ``water`` pixels; NaN elsewhere.

    ``water`` is both the population the median/MAD are taken from and the set of
    pixels that get a score, so passing a single zone's mask contrasts that zone
    against itself rather than against the whole body.
    """
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


def _px_dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.hypot(*(a - b)))


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


def zone_localised_clusters(
    rasters: dict[str, np.ndarray],
    zone_mask: np.ndarray,
    zone_id: str,
    transform: Affine,
    crs: str,
    *,
    indicators: list[str] | None = None,
    sigma_floors: dict[str, float],
    top_percent: float,
    z_floor: float,
    eps_px: float,
    min_samples: int,
    min_area_km2: float,
    max_clusters: int,
    max_spread_px: float,
    default_floor: float = 0.01,
) -> list[SpatialCluster]:
    """Hot patches *inside one zone*, contrasted against that zone's own water.

    For a zone the body-wide pass found nothing in. This asks a different
    question from ``spatial_detector``: significance was already established by
    the temporal detector against the seasonal baseline, so all that is left is
    *where inside the zone the indicator is worst* -- a percentile question, not
    a threshold one. A fixed z > 3 answers it badly: within a single zone that
    is only ~0.2% of the pixels (a few dozen, well under any sane area floor),
    so almost every zone came back empty and fell back to its own outline.

    So: take the zone's hottest ``top_percent`` of water pixels, but never below
    ``z_floor`` -- a genuinely uniform zone has a low percentile and correctly
    yields nothing, leaving the honest "zone-wide" answer.

    The result also has to be *compact*, not just small in area: the map fits the
    shape's bounding box, so two specks at opposite ends of a zone zoom it right
    back out to the whole zone. So the largest patch anchors the shape and only
    patches within ``max_spread_px`` of it join, at most ``max_clusters`` of
    them, from the single strongest indicator.
    """
    px_km2 = pixel_area_km2(transform)
    keys = [k for k in (indicators or list(rasters)) if k in rasters]
    per_indicator: list[tuple[float, list[SpatialCluster]]] = []
    for key in keys:
        z = pixel_z(rasters[key], zone_mask, sigma_floor=sigma_floors.get(key, default_floor))
        finite = np.isfinite(z)
        if int(finite.sum()) < min_samples:
            continue
        threshold = max(float(np.percentile(z[finite], 100.0 - top_percent)), z_floor)
        labels, skip, _frac = cluster_hot_pixels(
            z,
            z_threshold=threshold,
            eps_px=eps_px,
            min_samples=min_samples,
            # The percentile already caps the hot set at `top_percent`, so these
            # two guards cannot bite; they stay in the signature of the shared
            # helper, not in this pass's policy.
            max_hot_fraction=1.0,
            max_hot_pixels=int(np.iinfo(np.int32).max),
        )
        if skip:
            continue
        # Measure every cluster in pixel space first; polygonizing is the
        # expensive part and most clusters get dropped below.
        found: list[tuple[int, float, np.ndarray]] = []  # (label, area, centroid[row, col])
        for k in range(1, int(labels.max()) + 1):
            mask = labels == k
            n = int(mask.sum())
            if n * px_km2 < min_area_km2:
                continue
            rows, cols = np.nonzero(mask)
            found.append((k, n * px_km2, np.array([rows.mean(), cols.mean()])))
        if not found:
            continue
        found.sort(key=lambda t: t[1], reverse=True)
        # The largest patch anchors the shape; smaller ones join it only if they
        # are near it. Without this, two specks at opposite ends of the zone
        # produced a bounding box spanning the whole zone -- the map fits that
        # box, so the highlight was back to covering a third of the lake even
        # though its area was tiny.
        anchor = found[0]
        kept = [t for t in found[: max_clusters * 4] if _px_dist(t[2], anchor[2]) <= max_spread_px]
        kept = kept[:max_clusters]
        clusters = []
        for k, area, _c in kept:
            mask = labels == k
            zs = z[mask]
            clusters.append(
                SpatialCluster(
                    indicator=key,
                    zone_id=zone_id,
                    n_pixels=int(mask.sum()),
                    area_km2=area,
                    mean_z=float(np.nanmean(zs)),
                    max_z=float(np.nanmax(zs)),
                    geom_4326=_polygonize(labels, k, transform, crs),
                    # Clustered inside the zone mask, so every pixel is in it.
                    zone_pixel_share=1.0,
                )
            )
        if clusters:
            per_indicator.append((anchor[1], clusters))
    if not per_indicator:
        return []
    # One indicator's patches, not a union across indicators: different
    # indicators peak in different places, and merging them widens the shape
    # right back out. The strongest anchor wins; `indicator` on each cluster
    # records which.
    per_indicator.sort(key=lambda t: t[0], reverse=True)
    return per_indicator[0][1]


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
