"""L4 preprocessing: SCL-based cloud/shadow/cirrus masking and per-body scene rejection.

Everything here is pure NumPy on the shared 10 m grid produced by L3, so it is
cheap to unit test with synthetic arrays and never touches the network.

Sentinel-2 L2A Scene Classification (SCL) classes::

    0 NO_DATA   1 SATURATED_DEFECTIVE   2 DARK_AREA   3 CLOUD_SHADOW   4 VEGETATION
    5 NOT_VEGETATED   6 WATER   7 UNCLASSIFIED   8 CLOUD_MEDIUM   9 CLOUD_HIGH
    10 THIN_CIRRUS   11 SNOW_ICE
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.features import rasterize
from scipy import ndimage
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from app.services.l03_ingestion.reader import WindowedBands

# Masked out before any spectral work (dilated so cloud edges do not leak in).
SCL_CLOUD_CLASSES: frozenset[int] = frozenset({3, 8, 9, 10, 11})
# Not observations at all: nodata / saturated. Not dilated -- they are exact.
SCL_NODATA_CLASSES: frozenset[int] = frozenset({0, 1})
SCL_WATER = 6

CLOUD_DILATE_PX = 2
MIN_VALID_PCT = 40.0

_STRUCT_3X3 = np.ones((3, 3), dtype=bool)


def _isin(scl: np.ndarray, classes: frozenset[int]) -> np.ndarray:
    return np.asarray(np.isin(scl, np.fromiter(classes, dtype=scl.dtype)), dtype=bool)


def cloud_mask(scl: np.ndarray, dilate_px: int = CLOUD_DILATE_PX) -> np.ndarray:
    """True where SCL says cloud shadow / cloud / cirrus / snow, grown by ``dilate_px``."""
    mask = _isin(scl, SCL_CLOUD_CLASSES)
    if dilate_px > 0 and mask.any():
        mask = ndimage.binary_dilation(mask, structure=_STRUCT_3X3, iterations=dilate_px)
    return np.asarray(mask, dtype=bool)


def nodata_mask(scl: np.ndarray) -> np.ndarray:
    return _isin(scl, SCL_NODATA_CLASSES)


def valid_mask(scl: np.ndarray, dilate_px: int = CLOUD_DILATE_PX) -> np.ndarray:
    """Pixels that are real, cloud-free surface observations."""
    return np.asarray(~cloud_mask(scl, dilate_px) & ~nodata_mask(scl), dtype=bool)


def rasterize_aoi(
    geom_4326: BaseGeometry,
    crs: str,
    transform: Affine,
    shape: tuple[int, int],
    *,
    buffer_m: float = 0.0,
    all_touched: bool = False,
) -> np.ndarray:
    """Boolean raster of a WGS84 geometry on the band grid, optionally buffered (metres)."""
    fwd = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_user_input(crs), always_xy=True)
    projected = shp_transform(fwd.transform, geom_4326)
    if buffer_m:
        projected = projected.buffer(buffer_m)
    if projected.is_empty:
        return np.zeros(shape, dtype=bool)
    out = rasterize(
        [(projected, 1)],
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=all_touched,
    )
    return np.asarray(out, dtype=bool)


@dataclass
class Preprocessed:
    scene_id: str
    water_body_id: str
    aoi: np.ndarray  # bool, water body polygon on the grid
    valid: np.ndarray  # bool, cloud-free + not nodata (whole window)
    cloud: np.ndarray  # bool, dilated cloud/shadow/cirrus/snow
    aoi_pixels: int
    valid_pixel_pct: float  # % of AOI pixels that are valid
    cloud_pixel_pct: float  # % of AOI pixels under the dilated cloud mask
    usable: bool  # valid_pixel_pct >= threshold

    @property
    def valid_aoi(self) -> np.ndarray:
        return np.asarray(self.valid & self.aoi, dtype=bool)


def preprocess(
    bands: WindowedBands,
    aoi_4326: BaseGeometry,
    *,
    min_valid_pct: float = MIN_VALID_PCT,
    dilate_px: int = CLOUD_DILATE_PX,
) -> Preprocessed:
    """Build the validity masks for one cached scene window and decide usability
    for this water body. Callers must skip downstream work when ``usable`` is False."""
    scl = bands.arrays["SCL"]
    shape = (int(scl.shape[0]), int(scl.shape[1]))
    aoi = rasterize_aoi(aoi_4326, bands.crs, bands.transform, shape)
    cloud = cloud_mask(scl, dilate_px)
    valid = ~cloud & ~nodata_mask(scl)

    aoi_pixels = int(aoi.sum())
    if aoi_pixels == 0:
        valid_pct, cloud_pct = 0.0, 100.0
    else:
        valid_pct = 100.0 * float((valid & aoi).sum()) / aoi_pixels
        cloud_pct = 100.0 * float((cloud & aoi).sum()) / aoi_pixels

    return Preprocessed(
        scene_id=bands.scene_id,
        water_body_id=bands.water_body_id,
        aoi=aoi,
        valid=valid,
        cloud=cloud,
        aoi_pixels=aoi_pixels,
        valid_pixel_pct=round(valid_pct, 2),
        cloud_pixel_pct=round(cloud_pct, 2),
        usable=aoi_pixels > 0 and valid_pct >= min_valid_pct,
    )
