"""L5 water detection unit tests on a synthetic scene with a known water region."""

import io

import numpy as np
import pytest
import rasterio

from app.core.storage import MemoryStore
from app.services.l04_preprocessing.masks import preprocess
from app.services.l05_water_detection import mask as wm
from app.services.l05_water_detection.chips import (
    MASK_LAND,
    MASK_NODATA,
    MASK_WATER,
    chip_key,
    encode_water_mask,
    put_chip,
    read_chip,
)
from tests.test_preprocessing import CRS, SHAPE, TRANSFORM, make_bands, utm_box_4326

# Registered polygon: pixels [20, 60) x [30, 70). True water fills a slightly
# different rectangle so the mask must be derived from spectra, not the polygon.
AOI = utm_box_4326(20, 30, 60, 70)
WATER = (slice(32, 66), slice(22, 58))  # rows, cols -> 34 x 36 = 1224 px

# Typical L2A reflectance (x10000). Water: green > SWIR/NIR. Land: the reverse.
LAND = {"B03": 900, "B08": 2800, "B11": 2400}
WATER_PX = {"B03": 700, "B08": 300, "B11": 150}


def synthetic_scene(rng: np.random.Generator):  # type: ignore[no-untyped-def]
    scl = np.full(SHAPE, 4, dtype=np.uint8)
    scl[WATER] = 6
    arrays = {}
    for b in ("B03", "B08", "B11"):
        a = np.full(SHAPE, LAND[b], dtype=np.float64)
        a[WATER] = WATER_PX[b]
        a += rng.normal(0, 40, SHAPE)  # sensor noise
        arrays[b] = np.clip(a, 1, 10000).astype(np.uint16)
    return scl, arrays


def _speckle(arrays: dict[str, np.ndarray], rows: np.ndarray, cols: np.ndarray) -> None:
    for b in ("B03", "B08", "B11"):
        arrays[b][rows, cols] = WATER_PX[b]


def test_indices_and_safe_divide() -> None:
    b3 = np.array([[700, 0, 500]], dtype=np.uint16)
    b8 = np.array([[300, 0, 500]], dtype=np.uint16)
    nd = wm.normalized_difference(b3, b8)
    assert nd.dtype == np.float32
    assert nd[0, 0] == pytest.approx(0.4) and np.isnan(nd[0, 1]) and nd[0, 2] == 0.0


def test_otsu_splits_bimodal_and_falls_back_on_unimodal() -> None:
    rng = np.random.default_rng(1)
    bimodal = np.concatenate([rng.normal(-0.4, 0.05, 2000), rng.normal(0.5, 0.05, 2000)])
    t, method = wm.otsu_threshold(bimodal.astype(np.float32))
    assert method == "otsu" and -0.2 < t < 0.3
    unimodal = rng.normal(0.45, 0.02, 4000).astype(np.float32)  # all water, tiny spread
    assert wm.otsu_threshold(unimodal) == (0.0, "fallback")
    # Two wide land populations both below zero: Otsu would split land from land.
    land_only = np.concatenate([rng.normal(-0.7, 0.05, 2000), rng.normal(-0.45, 0.05, 2000)])
    t2, method2 = wm.otsu_threshold(land_only.astype(np.float32))
    assert method2 == "fallback" and t2 == 0.0
    assert wm.otsu_threshold(np.array([0.1, 0.2], dtype=np.float32))[1] == "fallback"


def test_clean_mask_removes_speckle_and_small_components() -> None:
    raw = np.zeros((50, 50), dtype=bool)
    raw[10:30, 10:30] = True  # 400 px blob
    raw[40, 40] = True  # isolated speckle
    raw[45:47, 5:7] = True  # 4 px blob -> removed by min size even if opening keeps it
    raw[20, 20] = False  # 1 px hole -> filled by closing
    cleaned, n = wm.clean_mask(raw, min_pixels=100)
    assert n == 1 and cleaned[10:30, 10:30].all() and cleaned.sum() == 400


def test_detect_water_recovers_known_region() -> None:
    rng = np.random.default_rng(7)
    scl, arrays = synthetic_scene(rng)
    # Speckle: 30 random single water-like pixels on land, away from the true region.
    rows = rng.integers(0, 25, 30)
    cols = rng.integers(0, 100, 30)
    _speckle(arrays, rows, cols)
    # "Flooded field next door": a 20 x 20 water blob well outside the 60 m buffer.
    field = (slice(80, 100), slice(70, 90))
    for b in ("B03", "B08", "B11"):
        arrays[b][field] = WATER_PX[b]
    scl[field] = 6

    bands = make_bands(scl, **arrays)
    pre = preprocess(bands, AOI)
    assert pre.usable
    result = wm.detect_water(bands, pre, AOI)

    assert result.threshold_method == "otsu"
    expected = np.zeros(SHAPE, dtype=bool)
    expected[WATER] = True
    assert np.array_equal(result.water, expected), f"diff={int((result.water ^ expected).sum())} px"
    assert result.water_pixels == 1224
    assert result.water_extent_km2 == pytest.approx(0.1224)
    assert result.n_components == 1
    assert not result.water[field].any()  # flooded field excluded by the polygon clip
    assert not result.water[rows, cols].any()  # speckle gone
    assert result.water_fraction_pct == pytest.approx(100 * 1224 / 1600, abs=0.01)


def test_detect_water_never_asserts_water_under_cloud() -> None:
    rng = np.random.default_rng(3)
    scl, arrays = synthetic_scene(rng)
    scl[40:45, 30:50] = 9  # a cloud bar across the water
    bands = make_bands(scl, **arrays)
    pre = preprocess(bands, AOI)
    result = wm.detect_water(bands, pre, AOI)
    assert result.threshold_method == "otsu"
    assert not (result.water & ~pre.valid).any()
    assert np.isnan(result.mndwi[42, 40])
    # Dilated cloud = rows 38..46 x cols 28..51 = 9 x 24 = 216 px removed from the 1224.
    assert result.water_pixels == 1224 - 9 * 24


def test_detect_water_all_land_gives_empty_mask() -> None:
    rng = np.random.default_rng(5)
    scl, arrays = synthetic_scene(rng)
    for b in ("B03", "B08", "B11"):
        arrays[b][:] = LAND[b]
    scl[:] = 5
    bands = make_bands(scl, **arrays)
    pre = preprocess(bands, AOI)
    result = wm.detect_water(bands, pre, AOI)
    assert result.threshold_method == "fallback"
    assert result.water_pixels == 0 and result.water_extent_km2 == 0.0


def test_seasonal_extent_difference_is_measurable() -> None:
    """The unit-level version of the acceptance test: same polygon, summer draw-down
    (small water rectangle) vs monsoon full pool (large one)."""
    rng = np.random.default_rng(11)

    def extent(water: tuple[slice, slice]) -> float:
        scl = np.full(SHAPE, 5, dtype=np.uint8)
        scl[water] = 6
        arrays = {}
        for b in ("B03", "B08", "B11"):
            a = np.full(SHAPE, LAND[b], dtype=np.float64)
            a[water] = WATER_PX[b]
            arrays[b] = np.clip(a + rng.normal(0, 40, SHAPE), 1, 10000).astype(np.uint16)
        bands = make_bands(scl, **arrays)
        return wm.detect_water(bands, preprocess(bands, AOI), AOI).water_extent_km2

    summer = extent((slice(45, 60), slice(35, 50)))  # 225 px
    monsoon = extent((slice(30, 70), slice(20, 60)))  # 1600 px
    assert summer == pytest.approx(0.0225) and monsoon == pytest.approx(0.16)
    assert monsoon > summer * 5


def test_water_mask_chip_round_trip_is_a_cog() -> None:
    rng = np.random.default_rng(9)
    scl, arrays = synthetic_scene(rng)
    scl[0:5, :] = 9
    bands = make_bands(scl, **arrays)
    pre = preprocess(bands, AOI)
    result = wm.detect_water(bands, pre, AOI)

    encoded = encode_water_mask(result.water, pre.valid)
    assert set(np.unique(encoded)) == {MASK_LAND, MASK_WATER, MASK_NODATA}
    assert (encoded[0:7, :] == MASK_NODATA).all()  # dilated cloud rows
    store = MemoryStore()
    from datetime import date

    key = chip_key("wb_synth", date(2026, 5, 3), "watermask")
    assert key == "chips/wb_synth/2026-05-03/body/watermask.tif"
    info = put_chip(
        store, key, encoded, TRANSFORM, CRS, bands.bounds, nodata=MASK_NODATA, tags={"k": "v"}
    )
    assert info.bytes < 20_000
    lon0, lat0, lon1, lat1 = info.bounds_4326
    assert 73 < lon0 < lon1 < 74 and 18 < lat0 < lat1 < 19

    data, transform, crs = read_chip(store, key)
    assert np.array_equal(data, encoded) and transform == TRANSFORM and crs == CRS
    with rasterio.open(io.BytesIO(store.get_bytes(key))) as src:
        assert src.nodata == MASK_NODATA and src.profile["tiled"]
        assert src.tags()["k"] == "v" and src.compression is not None
        assert src.overviews(1) == []  # 100 px fits in one 256 px block: none needed


def test_large_chip_gets_overviews() -> None:
    from affine import Affine

    from app.services.l05_water_detection.chips import write_cog

    data = np.zeros((700, 900), dtype=np.uint8)
    data[100:500, 200:800] = MASK_WATER
    blob = write_cog(data, Affine(10, 0, 360000, 0, -10, 2040000), CRS, nodata=MASK_NODATA)
    with rasterio.open(io.BytesIO(blob)) as src:
        assert src.overviews(1) == [2, 4]
        assert src.block_shapes == [(256, 256)]
