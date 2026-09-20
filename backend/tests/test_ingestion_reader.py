"""Reader tests against synthetic local GeoTIFFs (no network)."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box

from app.core.storage import MemoryStore
from app.services.l03_ingestion.cache import cache_key, load_bands, save_bands
from app.services.l03_ingestion.reader import (
    gdal_path,
    read_windowed_bands,
    snapped_window_bounds,
)
from app.services.l03_ingestion.stac import BANDS, SceneCandidate

# Synthetic tile: 200 x 200 px at 10 m, UTM 43N, origin (360000, 2040000) -> near Khadakwasla.
X0, Y0 = 360_000.0, 2_040_000.0
N10 = 200  # row*256+col must fit in uint16


def _write(path: Path, res: int, data: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs="EPSG:32643",
        transform=from_origin(X0, Y0, res, res),
        tiled=True,
        blockxsize=64,
        blockysize=64,
    ) as dst:
        dst.write(data, 1)


@pytest.fixture
def scene(tmp_path: Path) -> SceneCandidate:
    rows10, cols10 = np.indices((N10, N10))
    ten = (rows10 * 256 + cols10).astype(np.uint16)  # value encodes (row, col)
    rows20, cols20 = np.indices((N10 // 2, N10 // 2))
    twenty = (rows20 * 256 + cols20).astype(np.uint16)
    scl = ((rows20 + cols20) % 12).astype(np.uint8)  # categorical classes 0..11
    assets: dict[str, str] = {}
    for band in BANDS:
        p = tmp_path / f"{band}.tif"
        if band == "SCL":
            _write(p, 20, scl)
        elif band in ("B05", "B11"):
            _write(p, 20, twenty)
        else:
            _write(p, 10, ten)
        assets[band] = str(p)
    return SceneCandidate(
        id="SYNTH_43QCA_20260101_0_L2A",
        mgrs_tile="43QCA",
        sensed_at=datetime(2026, 1, 1, tzinfo=UTC),
        cloud_pct=0.0,
        platform="synthetic",
        stac_href="file://synthetic",
        source="test",
        assets=assets,
    )


def _aoi_4326(minx: float, miny: float, maxx: float, maxy: float):  # type: ignore[no-untyped-def]
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)
    lon0, lat0 = inv.transform(minx, miny)
    lon1, lat1 = inv.transform(maxx, maxy)
    return box(lon0, lat0, lon1, lat1)


def test_gdal_path_prefixes() -> None:
    assert gdal_path("https://a/b.tif") == "/vsicurl/https://a/b.tif"
    assert gdal_path("s3://bucket/k.tif") == "/vsis3/bucket/k.tif"
    assert gdal_path("/vsicurl/https://a/b.tif") == "/vsicurl/https://a/b.tif"
    assert gdal_path("C:/local.tif") == "C:/local.tif"


def test_snapped_bounds_expand_to_20m_grid_and_clip() -> None:
    t = Affine(10, 0, X0, 0, -10, Y0)
    raster = (X0, Y0 - 2000, X0 + 2000, Y0)
    out = snapped_window_bounds((X0 + 1005, Y0 - 2015, X0 + 1995, Y0 - 1005), t, raster)
    assert out == (X0 + 1000, Y0 - 2000, X0 + 2000, Y0 - 1000)  # bottom clipped to raster
    clipped = snapped_window_bounds((X0 - 500, Y0 - 500, X0 + 500, Y0 + 500), t, raster)
    assert clipped == (X0, Y0 - 500, X0 + 500, Y0)  # clipped to raster edge
    with pytest.raises(ValueError):
        snapped_window_bounds((X0 + 9000, Y0 - 9000, X0 + 9500, Y0 - 8500), t, raster)


def test_windowed_read_aligns_all_bands_on_one_10m_grid(scene: SceneCandidate) -> None:
    # AOI roughly 1 km x 0.8 km inside the tile; reader adds a 100 m buffer and snaps to 20 m.
    aoi = _aoi_4326(X0 + 1000, Y0 - 2000, X0 + 2000, Y0 - 1200)
    wb = read_windowed_bands(scene, aoi, "wb_test", buffer_m=0)

    assert wb.crs == "EPSG:32643"
    h, w = wb.shape
    assert all(a.shape == (h, w) for a in wb.arrays.values())
    assert wb.transform.a == 10 and wb.transform.e == -10
    # Window fully covers the AOI and starts on the 20 m grid.
    assert wb.bounds[0] <= X0 + 1000 and wb.bounds[2] >= X0 + 2000
    assert (wb.bounds[0] - X0) % 20 == 0 and (Y0 - wb.bounds[3]) % 20 == 0

    # 10 m band: values equal the source pixels at the window offset (no resampling).
    col_off = int((wb.bounds[0] - X0) / 10)
    row_off = int((Y0 - wb.bounds[3]) / 10)
    b04 = wb.arrays["B04"]
    assert int(b04[0, 0]) == row_off * 256 + col_off
    assert int(b04[-1, -1]) == (row_off + h - 1) * 256 + (col_off + w - 1)

    # 20 m band resampled to 10 m: each 2x2 block sits on its 20 m source pixel, and the
    # bilinear ramp stays within the neighbouring source values.
    b11 = wb.arrays["B11"]
    src_r, src_c = row_off // 2, col_off // 2
    assert abs(int(b11[1, 1]) - (src_r * 256 + src_c)) <= 256 + 1
    assert b11.dtype == np.uint16

    # SCL is nearest-neighbour: only integer classes 0..11 appear, as 2x2 blocks.
    scl = wb.arrays["SCL"]
    assert scl.dtype == np.uint8 and set(np.unique(scl)) <= set(range(12))
    assert np.array_equal(scl[0:2, 0:2], np.full((2, 2), scl[0, 0]))

    assert wb.bytes_read == sum(a.nbytes for a in wb.arrays.values())
    assert wb.duration_s >= 0


def test_windowed_read_rejects_non_10m_reference(scene: SceneCandidate) -> None:
    bad = SceneCandidate(
        **{**scene.__dict__, "assets": {**scene.assets, "B03": scene.assets["B11"]}}
    )
    with pytest.raises(ValueError, match="expected 10 m"):
        read_windowed_bands(bad, _aoi_4326(X0 + 1000, Y0 - 2000, X0 + 2000, Y0 - 1200), "wb")


def test_cache_roundtrip(scene: SceneCandidate) -> None:
    aoi = _aoi_4326(X0 + 1000, Y0 - 2000, X0 + 1500, Y0 - 1600)
    wb = read_windowed_bands(scene, aoi, "wb_test", buffer_m=0)
    store = MemoryStore()
    key = cache_key("cache", "wb_test", scene.id)
    assert key == "cache/wb_test/SYNTH_43QCA_20260101_0_L2A/bands.npz"
    size = save_bands(store, key, wb)
    assert store.exists(key) and 0 < size < wb.bytes_read  # compressed

    back = load_bands(store, key)
    assert back.scene_id == wb.scene_id and back.crs == wb.crs
    assert back.transform == wb.transform and back.bounds == wb.bounds
    for band in BANDS:
        assert np.array_equal(back.arrays[band], wb.arrays[band])
        assert back.arrays[band].dtype == wb.arrays[band].dtype
