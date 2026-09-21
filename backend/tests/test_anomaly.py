"""S6 pure-function tests: the three detectors, the rainfall gate and severity."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
from affine import Affine

from app.core.config import Settings
from app.services.l07_baseline.robust import build_seasonal_baseline
from app.services.l07_baseline.service import BaselineWindow
from app.services.l08_anomaly.gate import cap_severity, doy_percentile, rainfall_gate
from app.services.l08_anomaly.multivariate import SceneFeatures, multivariate_detector
from app.services.l08_anomaly.service import judge_zone, severity_from_votes
from app.services.l08_anomaly.spatial import pixel_z, spatial_detector
from app.services.l08_anomaly.temporal import temporal_detector

SETTINGS = Settings(rainfall_gate_min_history=5)
T0 = datetime(2024, 6, 1, tzinfo=UTC)


def _window(median: float, sigma: float, *, status: str = "usable") -> BaselineWindow:
    return BaselineWindow(
        zone_id="z1",
        indicator="x",
        doy_window=160,
        window_days=30,
        mean=median,
        std=sigma,
        p10=median - sigma,
        p90=median + sigma,
        n_samples=12,
        n_years=2,
        history_days=800,
        status=status,
        reason=None if status == "usable" else "building",
    )


def _series_baseline(values: list[float]) -> BaselineWindow:
    """Baseline built from a same-window synthetic history, as L7 would."""
    ts = [T0 + timedelta(days=i) for i in range(len(values))]
    w = build_seasonal_baseline(ts, values, window_days=30, doys=range(160, 161))[0]
    assert w.median is not None and w.sigma is not None
    return _window(w.median, w.sigma)


# --- temporal -------------------------------------------------------------------


def test_temporal_flags_only_the_spike() -> None:
    """Plan acceptance: 11 normal values and one 3x spike -> only the spike is flagged."""
    normals = [0.10, 0.11, 0.09, 0.10, 0.12, 0.10, 0.08, 0.11, 0.10, 0.09, 0.11]
    baseline = {"ndti_turbidity": _series_baseline(normals)}
    floors = SETTINGS.anomaly_sigma_floor
    for v in normals:
        r = temporal_detector({"ndti_turbidity": v}, baseline, z_threshold=3.0, sigma_floors=floors)
        assert not r.flagged, v
    spike = temporal_detector(
        {"ndti_turbidity": 0.30}, baseline, z_threshold=3.0, sigma_floors=floors
    )
    assert spike.flagged and spike.anomalous_indicators == ["ndti_turbidity"]
    assert spike.max_abs_z is not None and spike.max_abs_z > 3
    assert spike.scores["ndti_turbidity"].direction == "high"


def test_temporal_never_flags_against_building_baseline() -> None:
    r = temporal_detector(
        {"ndti_turbidity": 5.0},
        {"ndti_turbidity": _window(0.1, 0.01, status="building")},
        z_threshold=3.0,
        sigma_floors={},
    )
    assert not r.flagged and r.baseline_status == "building"
    assert r.scores["ndti_turbidity"].z is None


# --- rainfall gate --------------------------------------------------------------


def test_gate_downgrades_runoff_only_spike_and_spares_algae() -> None:
    d = rainfall_gate(["ndti_turbidity"], 80.0, 40.0, n_history=60, min_history=30)
    assert d.applied and d.exceeded
    assert cap_severity("high", d) == ("medium", "high")
    assert cap_severity("low", d) == ("low", None)
    # Algae under rain is still algae.
    d2 = rainfall_gate(
        ["ndti_turbidity", "ndci_chlorophyll"], 80.0, 40.0, n_history=60, min_history=30
    )
    assert not d2.applied and d2.exceeded and "ndci_chlorophyll" in d2.reason
    # No rain -> no gate; missing rain -> logged, not applied.
    assert not rainfall_gate(["ndti_turbidity"], 5.0, 40.0, n_history=60, min_history=30).applied
    thin = rainfall_gate(["ndti_turbidity"], 80.0, 40.0, n_history=3, min_history=30)
    assert not thin.applied and thin.exceeded is None
    assert not rainfall_gate(["ndti_turbidity"], None, 40.0, n_history=60, min_history=30).applied


def test_doy_percentile_uses_seasonal_window() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(366)]
    vals = [100.0 if 180 <= i <= 240 else 1.0 for i in range(366)]  # wet season Jul-Aug
    p90_monsoon, n = doy_percentile(days, vals, date(2025, 7, 25), window_days=30)
    p90_winter, _ = doy_percentile(days, vals, date(2025, 1, 15), window_days=30)
    assert p90_monsoon == 100.0 and p90_winter == 1.0 and n == 31


# --- severity + full zone verdict ----------------------------------------------


def test_severity_ladder() -> None:
    assert severity_from_votes(0, 10.0, z_high=5) is None
    assert severity_from_votes(1, 3.5, z_high=5) == "low"
    assert severity_from_votes(1, 6.0, z_high=5) == "medium"
    assert severity_from_votes(2, 3.5, z_high=5) == "medium"
    assert severity_from_votes(2, 6.0, z_high=5) == "high"
    assert severity_from_votes(3, None, z_high=5) == "high"


def test_judge_zone_gates_spike_when_rain_above_p90() -> None:
    """Plan acceptance: the same spike with rainfall above p90 -> natural_cause_likely."""
    normals = [0.10, 0.11, 0.09, 0.10, 0.12, 0.10, 0.08, 0.11, 0.10, 0.09, 0.11]
    baselines = {"ndti_turbidity": _series_baseline(normals)}
    values: dict[str, dict[str, float | None]] = {
        "ndti_turbidity": {"mean": 0.30, "water_fraction_pct": 90.0}
    }
    current = SceneFeatures(T0, {"ndti_turbidity": 0.30}, 90.0, None)
    dry = judge_zone(
        "z1",
        values,
        baselines,
        [],
        [],
        current,
        rainfall_72h=2.0,
        rainfall_p90=40.0,
        rainfall_n=60,
        settings=SETTINGS,
    )
    assert dry.severity in ("low", "medium") and not dry.natural_cause_likely and dry.alertable
    wet = judge_zone(
        "z1",
        values,
        baselines,
        [],
        [],
        current,
        rainfall_72h=95.0,
        rainfall_p90=40.0,
        rainfall_n=60,
        settings=SETTINGS,
    )
    assert wet.natural_cause_likely and wet.severity is not None
    assert wet.severity in ("low", "medium") and wet.gate.applied
    assert "runoff" in wet.gate.reason


def test_judge_zone_suppresses_when_baseline_building() -> None:
    baselines = {"ndti_turbidity": _window(0.1, 0.01, status="building")}
    values: dict[str, dict[str, float | None]] = {
        "ndti_turbidity": {"mean": 0.9, "water_fraction_pct": 90.0}
    }
    current = SceneFeatures(T0, {"ndti_turbidity": 0.9}, 90.0, 0.0)
    v = judge_zone(
        "z1",
        values,
        baselines,
        [],
        [],
        current,
        rainfall_72h=0.0,
        rainfall_p90=40.0,
        rainfall_n=60,
        settings=SETTINGS,
    )
    assert v.severity is None and not v.alertable and v.suppressed_reason == "no detector voted"


# --- spatial --------------------------------------------------------------------


def test_spatial_finds_plume_polygon_in_right_zone() -> None:
    rng = np.random.default_rng(1)
    h = w = 120
    water = np.ones((h, w), dtype=bool)
    ndti = rng.normal(0.10, 0.01, (h, w)).astype(np.float32)
    ndti[20:50, 20:50] += 0.15  # 30x30 px plume = 900 px = 0.09 km2 at 10 m
    transform = Affine(10.0, 0.0, 373000.0, 0.0, -10.0, 2050000.0)  # UTM 43N metres
    zone_masks = {"west": np.zeros((h, w), bool), "east": np.zeros((h, w), bool)}
    zone_masks["west"][:, :60] = True
    zone_masks["east"][:, 60:] = True
    res = spatial_detector(
        {"ndti_turbidity": ndti},
        water,
        zone_masks,
        transform,
        "EPSG:32643",
        sigma_floors={"ndti_turbidity": 0.005},
        z_threshold=3.0,
        eps_px=3.0,
        min_samples=10,
        min_area_km2=0.05,
        max_hot_fraction=0.30,
        max_hot_pixels=200_000,
    )
    assert len(res.clusters) == 1
    c = res.clusters[0]
    assert c.zone_id == "west" and 0.08 <= c.area_km2 <= 0.10 and c.max_z > 3
    lon, lat = c.geom_4326.centroid.x, c.geom_4326.centroid.y
    assert 73 < lon < 74 and 18 < lat < 19  # reprojected to WGS84 near Pune
    assert res.for_zone("east") == []


def test_spatial_skips_body_wide_shift() -> None:
    water = np.ones((50, 50), dtype=bool)
    vals = np.full((50, 50), 0.1, np.float32)
    vals[:, :20] = 0.5  # 40% of the body is "hot": a body-wide shift, not a plume
    z = pixel_z(vals, water, sigma_floor=0.005)
    assert np.isfinite(z).all()
    res = spatial_detector(
        {"ndti_turbidity": vals},
        water,
        {"z": water},
        Affine(10.0, 0.0, 0.0, 0.0, -10.0, 0.0),
        "EPSG:32643",
        sigma_floors={},
        z_threshold=3.0,
        eps_px=3.0,
        min_samples=10,
        min_area_km2=0.05,
        max_hot_fraction=0.30,
        max_hot_pixels=200_000,
    )
    assert res.clusters == [] and "body-wide" in res.skipped["ndti_turbidity"]


# --- multivariate ---------------------------------------------------------------


def _history(n: int, rng: np.random.Generator) -> list[SceneFeatures]:
    out = []
    for i in range(n):
        rain = float(rng.choice([0.0, 0.0, 0.0, 30.0]))
        out.append(
            SceneFeatures(
                observed_at=T0 + timedelta(days=5 * i),
                indicators={
                    "ndti_turbidity": 0.10 + 0.002 * rain + rng.normal(0, 0.01),
                    "ndci_chlorophyll": 0.02 + rng.normal(0, 0.01),
                    "fai_algal": 0.001 + rng.normal(0, 0.001),
                    "sediment_proxy": 0.03 + 0.001 * rain + rng.normal(0, 0.003),
                },
                water_fraction_pct=85.0 + rng.normal(0, 2),
                rainfall_72h=rain,
            )
        )
    return out


def test_multivariate_needs_history_then_flags_joint_outlier() -> None:
    rng = np.random.default_rng(3)
    hist = _history(40, rng)
    normal = SceneFeatures(T0 + timedelta(days=400), hist[-1].indicators, 85.0, 0.0)
    r = multivariate_detector(hist[:5], normal, min_history=20, contamination=0.05, seed=0)
    assert not r.fitted and r.reason and "needs 20" in r.reason
    r_ok = multivariate_detector(hist, normal, min_history=20, contamination=0.05, seed=0)
    assert r_ok.fitted and not r_ok.flagged
    odd = SceneFeatures(
        T0 + timedelta(days=400),
        {
            "ndti_turbidity": 0.25,
            "ndci_chlorophyll": 0.15,
            "fai_algal": 0.02,
            "sediment_proxy": 0.08,
        },
        85.0,
        0.0,  # all four up with no rain
    )
    r_odd = multivariate_detector(hist, odd, min_history=20, contamination=0.05, seed=0)
    assert r_odd.fitted and r_odd.flagged
    assert r_odd.score is not None and r_ok.score is not None and r_odd.score > r_ok.score


def test_multivariate_imputes_missing_rainfall() -> None:
    rng = np.random.default_rng(4)
    hist = _history(30, rng)
    cur = SceneFeatures(T0 + timedelta(days=300), hist[0].indicators, None, None)
    r = multivariate_detector(hist, cur, min_history=20, contamination=0.05, seed=0)
    assert r.fitted and set(r.imputed) == {"water_extent_delta", "rainfall_72h"}
