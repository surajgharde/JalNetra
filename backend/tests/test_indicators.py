"""L6 unit tests: indicator registry, reflectance conversion, clipping and zonal
aggregation on a synthetic scene with known water / land / cloud regions. No
network, no database."""

import numpy as np
import pytest

from app.services.l03_ingestion.reader import WindowedBands
from app.services.l04_preprocessing.masks import preprocess
from app.services.l05_water_detection.mask import detect_water
from app.services.l06_indicators import registry as reg
from app.services.l06_indicators.registry import (
    INDICATORS,
    QUALITY_INDICATOR_KEYS,
    describe_indicators,
    get_indicator,
    to_reflectance,
)
from app.services.l06_indicators.service import compute_indicator_rasters
from app.services.l06_indicators.zonal import (
    REJECT_CLOUDY,
    REJECT_NO_PIXELS,
    REJECT_TOO_FEW,
    ZoneRejection,
    ZoneStats,
    aggregate_all,
    aggregate_zone,
    rasterize_zones,
)
from tests.test_preprocessing import CRS, SHAPE, TRANSFORM, make_bands, utm_box_4326
from tests.test_water_mask import AOI, WATER


def dn(rho: float) -> int:
    """Reflectance -> L2A digital number (baseline >= 04.00)."""
    return round((rho + 0.1) * 10000)


# Two water populations inside the registered rectangle: a clear west half and a
# turbid, greener east half. Values are surface reflectance.
CLEAR = {"B03": 0.05, "B04": 0.03, "B05": 0.025, "B08": 0.012, "B11": 0.005}
TURBID = {"B03": 0.09, "B04": 0.11, "B05": 0.12, "B08": 0.06, "B11": 0.02}
LAND_RHO = {"B03": 0.09, "B04": 0.10, "B05": 0.16, "B08": 0.30, "B11": 0.26}
SPLIT_COL = 40  # water cols [22, 40) clear, [40, 58) turbid


def synthetic_bands(rng: np.random.Generator, cloud_rows: slice | None = None):  # type: ignore[no-untyped-def]
    scl = np.full(SHAPE, 4, dtype=np.uint8)
    scl[WATER] = 6
    arrays: dict[str, np.ndarray] = {}
    for b in CLEAR:
        a = np.full(SHAPE, dn(LAND_RHO[b]), dtype=np.float64)
        a[WATER] = dn(CLEAR[b])
        a[WATER[0], SPLIT_COL : WATER[1].stop] = dn(TURBID[b])
        a += rng.normal(0, 15, SHAPE)
        arrays[b] = np.clip(a, 1, 20000).astype(np.uint16)
    if cloud_rows is not None:
        scl[cloud_rows, :] = 9
    return make_bands(scl, **arrays)


def masks_for(bands: WindowedBands) -> tuple[np.ndarray, np.ndarray]:
    pre = preprocess(bands, AOI)
    wm = detect_water(bands, pre, AOI)
    return wm.water, pre.valid


# --- registry ----------------------------------------------------------------------


def test_registry_is_complete_and_documented() -> None:
    assert set(QUALITY_INDICATOR_KEYS) == {
        "ndti_turbidity",
        "ndci_chlorophyll",
        "fai_algal",
        "sediment_proxy",
    }
    assert "mndwi_extent" in INDICATORS and not INDICATORS["mndwi_extent"].water_only
    for key, ind in INDICATORS.items():
        assert ind.key == key
        assert ind.required_bands and all(
            b in ("B03", "B04", "B05", "B08", "B11") for b in ind.required_bands
        )
        lo, hi = ind.valid_range
        assert lo < hi
        # The UI renders this verbatim: it must name the proxy and its confounders.
        assert "proxy" in ind.scientific_basis.lower()
        assert "confounded" in ind.scientific_basis.lower()
        assert len(ind.scientific_basis) > 150
    with pytest.raises(KeyError):
        get_indicator("nope")
    described = describe_indicators()
    assert [d["key"] for d in described] == list(INDICATORS)
    assert described[0]["valid_range"] == [-1.0, 1.0]


def test_reflectance_conversion_removes_offset_and_clips() -> None:
    bands: dict[str, np.ndarray] = {
        "B04": np.array([[dn(0.03), 500, 30000]], dtype=np.uint16),
        "SCL": np.array([[6, 6, 6]], dtype=np.uint8),
    }
    rho = to_reflectance(bands)
    assert rho["B04"].dtype == np.float32
    assert rho["B04"][0, 0] == pytest.approx(0.03, abs=1e-6)
    assert rho["B04"][0, 1] == 0.0  # below the offset: dark water, clipped to 0
    assert rho["B04"][0, 2] == 1.0
    assert rho["SCL"] is bands["SCL"]
    legacy = to_reflectance(bands, offset=0)
    assert legacy["B04"][0, 1] == pytest.approx(0.05)


def test_indicator_formulas_on_known_reflectance() -> None:
    b = {k: np.array([[v]], dtype=np.float32) for k, v in CLEAR.items()}
    ndti = INDICATORS["ndti_turbidity"].compute(b)[0, 0]
    assert ndti == pytest.approx((0.03 - 0.05) / 0.08, abs=1e-5)
    ndci = INDICATORS["ndci_chlorophyll"].compute(b)[0, 0]
    assert ndci == pytest.approx((0.025 - 0.03) / 0.055, abs=1e-5)
    fai = INDICATORS["fai_algal"].compute(b)[0, 0]
    expected_fai = 0.012 - (0.03 + (0.005 - 0.03) * (842 - 665) / (1610 - 665))
    assert fai == pytest.approx(expected_fai, abs=1e-5)
    assert INDICATORS["sediment_proxy"].compute(b)[0, 0] == pytest.approx(0.03)
    mndwi = INDICATORS["mndwi_extent"].compute(b)[0, 0]
    assert mndwi == pytest.approx((0.05 - 0.005) / 0.055, abs=1e-5)

    # Turbid water ranks above clear water on turbidity and sediment.
    t = {k: np.array([[v]], dtype=np.float32) for k, v in TURBID.items()}
    assert INDICATORS["ndti_turbidity"].compute(t)[0, 0] > ndti
    assert INDICATORS["sediment_proxy"].compute(t)[0, 0] > 0.03


def test_offset_matters_for_ndti() -> None:
    """Skipping the BOA offset damps NDTI over water by 2-4x (more for darker
    water) -- the reason the registry works in reflectance rather than DN."""
    t = {k: np.array([[dn(v)]], dtype=np.uint16) for k, v in TURBID.items()}
    with_offset = INDICATORS["ndti_turbidity"].compute(to_reflectance(t))[0, 0]
    without = INDICATORS["ndti_turbidity"].compute(to_reflectance(t, offset=0))[0, 0]
    assert with_offset == pytest.approx(0.1, abs=1e-4)
    assert without == pytest.approx(with_offset / 2, abs=1e-4)


def test_clip_records_fraction_and_keeps_nan() -> None:
    ind = INDICATORS["sediment_proxy"]  # valid_range (0, 0.5)
    v = np.array([[0.1, 0.9, np.nan, -0.2]], dtype=np.float32)
    clipped, frac = ind.clip(v)
    assert clipped[0, 0] == pytest.approx(0.1) and clipped[0, 1] == 0.5 and clipped[0, 3] == 0.0
    assert np.isnan(clipped[0, 2])
    assert frac == pytest.approx(2 / 3)
    assert ind.clip(np.full((2, 2), np.nan, dtype=np.float32))[1] == 0.0


# --- rasters + zonal ---------------------------------------------------------------


def test_rasters_respect_water_and_valid_domains() -> None:
    rng = np.random.default_rng(3)
    bands = synthetic_bands(rng, cloud_rows=slice(60, 70))
    water, valid = masks_for(bands)
    rasters = compute_indicator_rasters(bands, water, valid, boa_offset=reg.BOA_ADD_OFFSET)
    assert set(rasters) == set(INDICATORS)

    ndti = rasters["ndti_turbidity"]
    assert ndti.values.dtype == np.float32 and ndti.values.shape == SHAPE
    finite = np.isfinite(ndti.values)
    assert finite.sum() == ndti.n_pixels
    assert not finite[~water].any()  # land is NaN for water-only indicators
    assert not finite[60:70].any()  # cloud rows are NaN everywhere
    assert finite[40, 30] and finite[40, 50]

    extent = rasters["mndwi_extent"]
    fin_ext = np.isfinite(extent.values)
    assert fin_ext[5, 5] and fin_ext[40, 30]  # land and water both carry a value
    assert not fin_ext[60:70].any()
    assert extent.values[5, 5] < 0 < extent.values[40, 30]

    # Clear vs turbid halves are separable on every quality indicator.
    for key in ("ndti_turbidity", "sediment_proxy", "ndci_chlorophyll"):
        v = rasters[key].values
        clear = np.nanmean(v[32:60, 22:40])
        turbid = np.nanmean(v[32:60, 40:58])
        assert turbid > clear, key
    assert -0.3 < np.nanmean(ndti.values[32:60, 22:40]) < 0.5
    assert all(r.clipped_pct == 0.0 for r in rasters.values())


def test_zonal_stats_mean_p90_and_quality_figures() -> None:
    rng = np.random.default_rng(4)
    bands = synthetic_bands(rng)
    water, valid = masks_for(bands)
    rasters = compute_indicator_rasters(bands, water, valid, boa_offset=reg.BOA_ADD_OFFSET)
    zones = [
        ("z_west", utm_box_4326(20, 30, 40, 70)),  # clear half + some land
        ("z_east", utm_box_4326(40, 30, 60, 70)),  # turbid half + some land
        ("z_land", utm_box_4326(0, 0, 10, 10)),  # no water at all
        ("z_off", utm_box_4326(500, 500, 510, 510)),  # off the grid
    ]
    zr = rasterize_zones(zones, CRS, TRANSFORM, valid, water)
    by_id = {z.zone_id: z for z in zr}
    assert by_id["z_west"].pixels == 800 and by_id["z_west"].valid_pixel_pct == 100.0
    assert 0 < by_id["z_west"].water_fraction_pct < 100
    assert by_id["z_land"].water_pixels == 0 and by_id["z_off"].pixels == 0

    stats, rejected = aggregate_all(
        zr, {k: (r.indicator, r.values, r.clipped) for k, r in rasters.items()}
    )
    got = {(s.zone_id, s.indicator): s for s in stats}
    rej = {(r.zone_id, r.indicator): r for r in rejected}
    assert len(got) + len(rej) == 4 * len(INDICATORS)

    west, east = got[("z_west", "ndti_turbidity")], got[("z_east", "ndti_turbidity")]
    assert east.mean > west.mean and east.p90 >= east.mean and west.std < 0.1
    assert west.n_pixels > 0 and west.valid_pixel_pct == 100.0 and west.clipped_pct == 0.0
    assert west.water_fraction_pct == pytest.approx(by_id["z_west"].water_fraction_pct)
    # Extent is computed over the whole zone, so its pixel count is the zone size.
    assert got[("z_west", "mndwi_extent")].n_pixels == 800

    # A dry zone has nothing to aggregate for water-only indicators but still tracks extent.
    assert rej[("z_land", "ndti_turbidity")].reason == REJECT_TOO_FEW
    assert ("z_land", "mndwi_extent") in got and got[("z_land", "mndwi_extent")].mean < 0
    assert all(rej[("z_off", k)].reason == REJECT_NO_PIXELS for k in INDICATORS)


def test_cloudy_zone_is_rejected_by_valid_pct() -> None:
    rng = np.random.default_rng(5)
    bands = synthetic_bands(rng, cloud_rows=slice(30, 58))  # covers most of the west zone
    water, valid = masks_for(bands)
    rasters = compute_indicator_rasters(bands, water, valid, boa_offset=reg.BOA_ADD_OFFSET)
    zr = rasterize_zones([("z_west", utm_box_4326(20, 30, 40, 70))], CRS, TRANSFORM, valid, water)
    ind = INDICATORS["ndti_turbidity"]
    r = rasters[ind.key]
    out = aggregate_zone(zr[0], ind, r.values, r.clipped)
    assert isinstance(out, ZoneRejection) and out.reason == REJECT_CLOUDY
    assert out.valid_pixel_pct < 30
    # With a lenient threshold the same zone aggregates fine on the visible strip.
    ok = aggregate_zone(zr[0], ind, r.values, r.clipped, min_valid_pct=5.0)
    assert isinstance(ok, ZoneStats) and ok.valid_pixel_pct == out.valid_pixel_pct


def test_clipped_fraction_is_reported_per_zone() -> None:
    rng = np.random.default_rng(6)
    bands = synthetic_bands(rng)
    # Blow out the red band on part of the turbid half: sediment proxy > 0.5.
    bands.arrays["B04"][40:50, 45:55] = dn(0.8)
    water, valid = masks_for(bands)
    rasters = compute_indicator_rasters(bands, water, valid, boa_offset=reg.BOA_ADD_OFFSET)
    sed = rasters["sediment_proxy"]
    assert sed.clipped_pct > 0 and np.nanmax(sed.values) == 0.5
    zr = rasterize_zones([("z", AOI)], CRS, TRANSFORM, valid, water)
    s = aggregate_zone(zr[0], sed.indicator, sed.values, sed.clipped)
    assert isinstance(s, ZoneStats) and s.clipped_pct > 0 and s.p90 <= 0.5
