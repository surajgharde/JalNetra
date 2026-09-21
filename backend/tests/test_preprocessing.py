"""L4 preprocessing unit tests on synthetic SCL arrays (no network, no DB)."""

import numpy as np
from affine import Affine
from shapely.geometry import box

from app.services.l03_ingestion.reader import WindowedBands
from app.services.l03_ingestion.stac import BANDS
from app.services.l04_preprocessing.masks import (
    cloud_mask,
    nodata_mask,
    preprocess,
    rasterize_aoi,
    valid_mask,
)
from app.services.registry.geo import to_wgs84, utm_crs_for

# A 100 x 100 px window at 10 m in UTM 43N, origin at (360000, 2040000).
ORIGIN_X, ORIGIN_Y = 360000.0, 2040000.0
TRANSFORM = Affine(10, 0, ORIGIN_X, 0, -10, ORIGIN_Y)
CRS = "EPSG:32643"
SHAPE = (100, 100)


def utm_box_4326(col0: int, row0: int, col1: int, row1: int):  # type: ignore[no-untyped-def]
    """Pixel-aligned rectangle [col0, col1) x [row0, row1) as a WGS84 polygon."""
    minx, maxx = ORIGIN_X + col0 * 10, ORIGIN_X + col1 * 10
    maxy, miny = ORIGIN_Y - row0 * 10, ORIGIN_Y - row1 * 10
    poly = box(minx, miny, maxx, maxy)
    from pyproj import CRS as PCRS

    return to_wgs84(poly, PCRS.from_user_input(CRS))


def make_bands(scl: np.ndarray, **extra: np.ndarray) -> WindowedBands:
    arrays = {b: np.full(scl.shape, 1000, dtype=np.uint16) for b in BANDS if b != "SCL"}
    arrays.update(extra)
    arrays["SCL"] = scl.astype(np.uint8)
    return WindowedBands(
        scene_id="TEST_SYNTH",
        water_body_id="wb_synth",
        crs=CRS,
        transform=TRANSFORM,
        bounds=(ORIGIN_X, ORIGIN_Y - SHAPE[0] * 10, ORIGIN_X + SHAPE[1] * 10, ORIGIN_Y),
        arrays=arrays,
        bytes_read=0,
        duration_s=0.0,
    )


def test_cloud_mask_classes_and_dilation() -> None:
    scl = np.full((20, 20), 6, dtype=np.uint8)  # all water
    scl[10, 10] = 9  # one high-probability cloud pixel
    undilated = cloud_mask(scl, dilate_px=0)
    assert undilated.sum() == 1
    dilated = cloud_mask(scl)  # default 2 px -> 5 x 5 block
    assert dilated.sum() == 25
    assert dilated[8:13, 8:13].all() and not dilated[7, 7] and not dilated[13, 13]

    for cls in (3, 8, 9, 10, 11):
        assert cloud_mask(np.array([[cls]], dtype=np.uint8), dilate_px=0)[0, 0]
    for cls in (2, 4, 5, 6, 7):
        assert not cloud_mask(np.array([[cls]], dtype=np.uint8), dilate_px=0)[0, 0]


def test_nodata_not_dilated_and_valid_combines_both() -> None:
    scl = np.full((10, 10), 4, dtype=np.uint8)
    scl[0, 0] = 0  # nodata
    scl[5, 5] = 8  # cloud
    assert nodata_mask(scl).sum() == 1
    valid = valid_mask(scl)
    assert not valid[0, 0] and not valid[5, 5] and not valid[3, 3]  # 3,3 is in the dilation
    assert valid.sum() == 100 - 1 - 25


def test_rasterize_aoi_is_pixel_exact_and_buffer_grows_it() -> None:
    aoi = utm_box_4326(20, 30, 60, 70)  # 40 x 40 px
    r = rasterize_aoi(aoi, CRS, TRANSFORM, SHAPE)
    assert r.sum() == 1600 and r[30:70, 20:60].all()
    r60 = rasterize_aoi(aoi, CRS, TRANSFORM, SHAPE, buffer_m=60)
    # +6 px on every side (corners rounded by shapely's buffer)
    assert r60.sum() > 1600 and r60[24:76, 20:60].all() and not r60[23, 40]


def test_preprocess_rejects_cloudy_scene_over_aoi_only() -> None:
    aoi = utm_box_4326(20, 30, 60, 70)  # 1600 px
    scl = np.full(SHAPE, 6, dtype=np.uint8)
    # Cloud the top 70 % of the AOI rows (rows 30..57); dilation adds 2 rows -> 30 of 40.
    scl[30:58, 20:60] = 9
    pre = preprocess(make_bands(scl), aoi)
    assert pre.aoi_pixels == 1600
    assert pre.valid_pixel_pct == 25.0  # rows 60..69 remain valid = 10/40
    assert not pre.usable

    # Same cloud but outside the AOI -> unaffected.
    scl2 = np.full(SHAPE, 6, dtype=np.uint8)
    scl2[0:20, :] = 9
    pre2 = preprocess(make_bands(scl2), aoi)
    assert pre2.valid_pixel_pct == 100.0 and pre2.usable and pre2.cloud_pixel_pct == 0.0


def test_preprocess_threshold_is_inclusive_and_configurable() -> None:
    aoi = utm_box_4326(0, 0, 100, 50)  # top half, 5000 px
    scl = np.full(SHAPE, 6, dtype=np.uint8)
    scl[0:28, :] = 9  # 28 rows + 2 dilated = 30 of 50 -> 40 % valid
    pre = preprocess(make_bands(scl), aoi)
    assert pre.valid_pixel_pct == 40.0 and pre.usable
    assert not preprocess(make_bands(scl), aoi, min_valid_pct=50).usable


def test_preprocess_with_aoi_outside_window_is_unusable() -> None:
    far = utm_box_4326(500, 500, 520, 520)
    pre = preprocess(make_bands(np.full(SHAPE, 6, dtype=np.uint8)), far)
    assert pre.aoi_pixels == 0 and not pre.usable


def test_utm_helper_matches_grid_crs() -> None:
    assert utm_crs_for(utm_box_4326(0, 0, 10, 10)).to_epsg() == 32643
