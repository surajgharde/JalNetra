"""Cloud-Optimized GeoTIFF chips in object storage.

Layout (data model): ``chips/{water_body_id}/{scene_date}/{scope}/{layer}.tif`` where
``scope`` is a zone id, or ``body`` for whole-water-body layers such as the water
mask. Chips are written through GDAL's COG driver (tiled, deflate, overviews) so
TiTiler can serve them straight from MinIO in S9.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date

import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile

from app.core.storage import ObjectStore

CHIP_PREFIX = "chips"
BODY_SCOPE = "body"

# Water mask chip encoding. 255 keeps "unknown" distinct from "land" for display and stats.
MASK_LAND = 0
MASK_WATER = 1
MASK_NODATA = 255


def chip_key(
    water_body_id: str,
    scene_date: date,
    layer: str,
    scope: str = BODY_SCOPE,
    prefix: str = CHIP_PREFIX,
) -> str:
    return f"{prefix}/{water_body_id}/{scene_date.isoformat()}/{scope}/{layer}.tif"


@dataclass(frozen=True)
class ChipInfo:
    key: str
    bytes: int
    bounds_4326: tuple[float, float, float, float]  # minx, miny, maxx, maxy (lon/lat)


def encode_water_mask(water: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.full(water.shape, MASK_NODATA, dtype=np.uint8)
    out[valid] = MASK_LAND
    out[water & valid] = MASK_WATER
    return out


def bounds_to_4326(
    bounds: tuple[float, float, float, float], crs: str
) -> tuple[float, float, float, float]:
    fwd = Transformer.from_crs(CRS.from_user_input(crs), CRS.from_epsg(4326), always_xy=True)
    minx, miny, maxx, maxy = bounds
    xs, ys = fwd.transform([minx, maxx, minx, maxx], [miny, miny, maxy, maxy])
    return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))


def write_cog(
    data: np.ndarray,
    transform: Affine,
    crs: str,
    *,
    nodata: int | float | None,
    tags: dict[str, str] | None = None,
) -> bytes:
    """Serialize a single-band array as an in-memory COG."""
    if data.ndim != 2:
        raise ValueError("chips are single-band")
    profile = {
        "driver": "COG",
        "dtype": data.dtype.name,
        "width": data.shape[1],
        "height": data.shape[0],
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "compress": "DEFLATE",
        "predictor": 2 if data.dtype.kind in "iu" else 3,
        "blocksize": 256,
        "overview_resampling": "nearest" if data.dtype.kind in "iu" else "average",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(data, 1)
            if tags:
                dst.update_tags(**tags)
        return bytes(mem.read())


def put_chip(
    store: ObjectStore,
    key: str,
    data: np.ndarray,
    transform: Affine,
    crs: str,
    bounds: tuple[float, float, float, float],
    *,
    nodata: int | float | None,
    tags: dict[str, str] | None = None,
) -> ChipInfo:
    blob = write_cog(data, transform, crs, nodata=nodata, tags=tags)
    store.put_bytes(key, blob, content_type="image/tiff")
    return ChipInfo(key=key, bytes=len(blob), bounds_4326=bounds_to_4326(bounds, crs))


def read_chip(store: ObjectStore, key: str) -> tuple[np.ndarray, Affine, str]:
    with rasterio.open(io.BytesIO(store.get_bytes(key))) as src:
        return src.read(1), src.transform, str(src.crs)
