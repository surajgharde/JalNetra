"""L5 water detection: adaptive MNDWI water mask for one scene window.

Pipeline (all on the shared 10 m grid):

1. NDWI = (B3 - B8) / (B3 + B8), MNDWI = (B3 - B11) / (B3 + B11), float32, NaN
   where the denominator is zero or the pixel is invalid (cloud / nodata).
2. Otsu threshold on the MNDWI histogram of valid pixels inside the search area
   (body polygon + 60 m) -- adapts to season and turbidity. Falls back to 0.0
   when the histogram is effectively unimodal (all water or all land), where
   Otsu's split is meaningless.
3. Binary opening then closing with a 3x3 structuring element (speckle).
4. Keep connected components >= ``min_component_km2`` (default 0.01 km2 = 100 px).
5. Intersect with the registered polygon buffered by 60 m so a flooded field
   next door is not counted as the reservoir.

Water under cloud is *unknown*, not "no water": ``water`` is only ever True on
valid pixels, and ``water_extent_km2`` is therefore a visible-extent figure.
Downstream layers must read it together with ``valid_pixel_pct``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from shapely.geometry.base import BaseGeometry

from app.services.l03_ingestion.reader import GRID_M, WindowedBands
from app.services.l04_preprocessing.masks import Preprocessed, rasterize_aoi

BODY_BUFFER_M = 60.0
MIN_COMPONENT_KM2 = 0.01
PIXEL_KM2 = (GRID_M * GRID_M) / 1e6  # 1e-4 km2 per 10 m pixel

# Otsu is only trusted inside this band; outside it the histogram was unimodal.
OTSU_MIN, OTSU_MAX = -0.35, 0.55
FALLBACK_THRESHOLD = 0.0
# Otsu needs two populations; below this spread it is fitting noise.
MIN_STD_FOR_OTSU = 0.05
MIN_PIXELS_FOR_OTSU = 50

_STRUCT_3X3 = np.ones((3, 3), dtype=bool)


def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b) as float32; NaN where a + b == 0."""
    a32 = a.astype(np.float32, copy=False)
    b32 = b.astype(np.float32, copy=False)
    denom = a32 + b32
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(denom != 0, (a32 - b32) / denom, np.nan)
    return np.asarray(out, dtype=np.float32)


def ndwi(bands: dict[str, np.ndarray]) -> np.ndarray:
    return normalized_difference(bands["B03"], bands["B08"])


def mndwi(bands: dict[str, np.ndarray]) -> np.ndarray:
    return normalized_difference(bands["B03"], bands["B11"])


def otsu(values: np.ndarray, nbins: int = 256) -> float:
    """Otsu's threshold (max between-class variance) on a 1-D sample.

    When the two modes are cleanly separated the variance curve is flat across the
    empty gap; the usual "first argmax" then sits right next to the lower mode and
    lets noisy land pixels leak into water. We return the midpoint of the maximal
    plateau instead.
    """
    hist, edges = np.histogram(values, bins=nbins)
    centers = (edges[:-1] + edges[1:]) / 2
    w = hist.astype(np.float64)
    w0 = np.cumsum(w)
    w1 = w0[-1] - w0
    m0 = np.cumsum(w * centers)
    total_mean = m0[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        mu0 = m0 / w0
        mu1 = (total_mean - m0) / w1
        between = w0 * w1 * (mu0 - mu1) ** 2
    between = np.nan_to_num(between[:-1], nan=-1.0)  # last split has an empty class 1
    best = np.flatnonzero(between >= between.max() - 1e-12 * abs(between.max()))
    idx = round((best[0] + best[-1]) / 2)
    return float(edges[idx + 1])  # upper edge of the split bin


def otsu_threshold(values: np.ndarray) -> tuple[float, str]:
    """Adaptive MNDWI split. Returns (threshold, method) where method is
    ``"otsu"`` or ``"fallback"`` so the decision is auditable per scene."""
    v = values[np.isfinite(values)]
    if v.size < MIN_PIXELS_FOR_OTSU or float(np.std(v)) < MIN_STD_FOR_OTSU:
        return FALLBACK_THRESHOLD, "fallback"
    t = otsu(v)
    if not (OTSU_MIN <= t <= OTSU_MAX):
        return FALLBACK_THRESHOLD, "fallback"
    return round(t, 4), "otsu"


def clean_mask(raw: np.ndarray, min_pixels: int) -> tuple[np.ndarray, int]:
    """3x3 opening then closing, then drop components smaller than ``min_pixels``.
    Returns (mask, number of components kept)."""
    m = ndimage.binary_opening(raw, structure=_STRUCT_3X3)
    m = ndimage.binary_closing(m, structure=_STRUCT_3X3)
    labels, n = ndimage.label(m, structure=_STRUCT_3X3)
    if n == 0:
        return np.zeros_like(raw, dtype=bool), 0
    sizes = ndimage.sum_labels(
        np.ones_like(labels, dtype=np.int32), labels, index=np.arange(1, n + 1)
    )
    keep = np.flatnonzero(np.asarray(sizes) >= min_pixels) + 1
    out = np.isin(labels, keep)
    return np.asarray(out, dtype=bool), int(keep.size)


@dataclass
class WaterMask:
    scene_id: str
    water_body_id: str
    water: np.ndarray  # bool; True only on valid pixels inside the buffered body
    ndwi: np.ndarray  # float32, NaN on invalid pixels
    mndwi: np.ndarray  # float32, NaN on invalid pixels
    search_area: np.ndarray  # bool; body polygon + 60 m
    threshold: float
    threshold_method: str  # otsu | fallback
    water_pixels: int
    water_extent_km2: float
    n_components: int
    # % of the *registered* polygon that is visible water. Complements valid_pixel_pct.
    water_fraction_pct: float


def detect_water(
    bands: WindowedBands,
    pre: Preprocessed,
    aoi_4326: BaseGeometry,
    *,
    buffer_m: float = BODY_BUFFER_M,
    min_component_km2: float = MIN_COMPONENT_KM2,
) -> WaterMask:
    arrays = bands.arrays
    shape = pre.valid.shape
    search = rasterize_aoi(aoi_4326, bands.crs, bands.transform, shape, buffer_m=buffer_m)

    nd = ndwi(arrays)
    mnd = mndwi(arrays)
    nd[~pre.valid] = np.nan
    mnd[~pre.valid] = np.nan

    threshold, method = otsu_threshold(mnd[search & pre.valid])
    with np.errstate(invalid="ignore"):
        raw = (mnd > threshold) & pre.valid
    min_pixels = max(1, round(min_component_km2 / PIXEL_KM2))
    cleaned, _ = clean_mask(raw, min_pixels)
    # Closing can bridge across cloud pixels; never assert water where we cannot see.
    water = cleaned & search & pre.valid
    _, n_components = ndimage.label(water, structure=_STRUCT_3X3)

    water_pixels = int(water.sum())
    aoi_pixels = pre.aoi_pixels
    fraction = 100.0 * float((water & pre.aoi).sum()) / aoi_pixels if aoi_pixels else 0.0
    return WaterMask(
        scene_id=bands.scene_id,
        water_body_id=bands.water_body_id,
        water=water,
        ndwi=nd,
        mndwi=mnd,
        search_area=search,
        threshold=threshold,
        threshold_method=method,
        water_pixels=water_pixels,
        water_extent_km2=round(water_pixels * PIXEL_KM2, 4),
        n_components=n_components,
        water_fraction_pct=round(fraction, 2),
    )
