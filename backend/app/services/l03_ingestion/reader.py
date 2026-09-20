"""Windowed band reads from remote Cloud-Optimized GeoTIFFs.

Never downloads a whole product. Each band is opened through GDAL's ``/vsicurl/``
driver and only the block range covering the water body's bounding box is
fetched with HTTP range requests. All bands are returned on one shared 10 m grid:
20 m bands are resampled bilinearly (SCL with nearest, since it is categorical).

The AOI window is snapped outward to the 20 m grid of the tile so the 10 m and
20 m windows describe exactly the same ground footprint and align pixel-for-pixel.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.windows import Window, from_bounds
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.services.l03_ingestion.stac import BANDS, SceneCandidate

log = logging.getLogger(__name__)

# The only GDAL settings that matter for cost: no directory listing on open, range
# requests only on the asset itself, and merged/multiplexed HTTP ranges.
GDAL_ENV: dict[str, str | int | bool] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.jp2",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": str(64 * 1024 * 1024),
    "CPL_VSIL_CURL_CACHE_SIZE": str(64 * 1024 * 1024),
    "GDAL_CACHEMAX": 256,  # MB; rasterio requires an int here
}

GRID_M = 10  # target grid
SNAP_M = 20  # snap the window to the coarsest band grid so all windows align
BUFFER_M = 100  # margin around the water body so S3's 60 m buffer stays inside


@dataclass
class WindowedBands:
    scene_id: str
    water_body_id: str
    crs: str  # e.g. "EPSG:32643"
    transform: Affine  # of the shared 10 m grid
    bounds: tuple[float, float, float, float]  # minx, miny, maxx, maxy in `crs`
    arrays: dict[str, np.ndarray]  # canonical band -> (H, W) array
    bytes_read: int  # uncompressed bytes materialised; upper bound on transfer
    duration_s: float

    @property
    def shape(self) -> tuple[int, int]:
        first = next(iter(self.arrays.values()))
        return int(first.shape[0]), int(first.shape[1])


def gdal_path(href: str) -> str:
    if href.startswith("/vsi"):
        return href
    if href.startswith(("http://", "https://")):
        return f"/vsicurl/{href}"
    if href.startswith("s3://"):
        return "/vsis3/" + href.removeprefix("s3://")
    return href


def snapped_window_bounds(
    aoi_bounds: tuple[float, float, float, float],
    transform: Affine,
    raster_bounds: tuple[float, float, float, float],
    snap_m: float = SNAP_M,
) -> tuple[float, float, float, float]:
    """Expand AOI bounds outward to the raster's snap_m grid and clip to the raster."""
    x0, y0 = transform.c, transform.f  # raster origin (upper-left)
    minx, miny, maxx, maxy = aoi_bounds
    minx_s = x0 + math.floor((minx - x0) / snap_m) * snap_m
    maxx_s = x0 + math.ceil((maxx - x0) / snap_m) * snap_m
    maxy_s = y0 - math.floor((y0 - maxy) / snap_m) * snap_m
    miny_s = y0 - math.ceil((y0 - miny) / snap_m) * snap_m
    rminx, rminy, rmaxx, rmaxy = raster_bounds
    out = (max(minx_s, rminx), max(miny_s, rminy), min(maxx_s, rmaxx), min(maxy_s, rmaxy))
    if out[0] >= out[2] or out[1] >= out[3]:
        raise ValueError("AOI does not intersect the raster")
    return out


def _aoi_bounds_in(
    crs: CRS, aoi_4326: BaseGeometry, buffer_m: float
) -> tuple[float, float, float, float]:
    fwd = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True).transform
    projected = shp_transform(fwd, aoi_4326).buffer(buffer_m)
    minx, miny, maxx, maxy = projected.bounds
    return float(minx), float(miny), float(maxx), float(maxy)


_retry = retry(
    retry=retry_if_exception_type((RasterioIOError, OSError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)


@_retry
def _read_band(
    path: str,
    bounds: tuple[float, float, float, float],
    out_shape: tuple[int, int],
    resampling: Resampling,
    env: dict[str, str | int | bool],
) -> tuple[np.ndarray, Any]:
    with rasterio.Env(**env), rasterio.open(path) as src:
        win = from_bounds(*bounds, transform=src.transform)
        win = Window(round(win.col_off), round(win.row_off), round(win.width), round(win.height))
        data = src.read(1, window=win, out_shape=out_shape, resampling=resampling)
        return data, src.dtypes[0]


def read_windowed_bands(
    candidate: SceneCandidate,
    aoi_4326: BaseGeometry,
    water_body_id: str,
    *,
    buffer_m: float = BUFFER_M,
    bands: tuple[str, ...] = BANDS,
) -> WindowedBands:
    started = time.perf_counter()
    env: dict[str, str | int | bool] = {**GDAL_ENV, **candidate.gdal_env}

    # Reference grid from a 10 m band.
    ref_band = next(b for b in bands if b != "SCL" and b in ("B03", "B04", "B08"))
    with rasterio.Env(**env), rasterio.open(gdal_path(candidate.assets[ref_band])) as ref:
        crs = CRS.from_user_input(ref.crs)
        aoi_bounds = _aoi_bounds_in(crs, aoi_4326, buffer_m)
        bounds = snapped_window_bounds(aoi_bounds, ref.transform, tuple(ref.bounds))
        res = float(ref.res[0])
        if abs(res - GRID_M) > 1e-6:
            raise ValueError(f"reference band {ref_band} is {res} m, expected {GRID_M} m")

    width = round((bounds[2] - bounds[0]) / GRID_M)
    height = round((bounds[3] - bounds[1]) / GRID_M)
    out_shape = (height, width)
    transform = Affine(GRID_M, 0.0, bounds[0], 0.0, -GRID_M, bounds[3])

    arrays: dict[str, np.ndarray] = {}
    for band in bands:
        resampling = Resampling.nearest if band == "SCL" else Resampling.bilinear
        data, _dtype = _read_band(
            gdal_path(candidate.assets[band]), bounds, out_shape, resampling, env
        )
        arrays[band] = data.astype(np.uint8 if band == "SCL" else np.uint16, copy=False)

    result = WindowedBands(
        scene_id=candidate.id,
        water_body_id=water_body_id,
        crs=f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_wkt(),
        transform=transform,
        bounds=bounds,
        arrays=arrays,
        bytes_read=int(sum(a.nbytes for a in arrays.values())),
        duration_s=round(time.perf_counter() - started, 3),
    )
    log.info(
        "windowed read complete",
        extra={
            "scene_id": candidate.id,
            "water_body_id": water_body_id,
            "shape": list(out_shape),
            "bytes_read": result.bytes_read,
            "duration_s": result.duration_s,
            "source": candidate.source,
        },
    )
    return result
