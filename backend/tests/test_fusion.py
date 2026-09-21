"""S7 pure-function tests: feature vector, weighted model, contributions, summary."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.services.l09_fusion.features import (
    FEATURE_NAMES,
    build_features,
    rainfall_percentile_rank,
    spatial_expansion,
)
from app.services.l09_fusion.models import WEIGHTS_V1, WeightedModel
from app.services.l09_fusion.service import (
    apply_rainfall_cap,
    confidence_from,
    severity_from_score,
)
from app.services.l10_explain.explain import (
    DISCLAIMER,
    build_summary,
    check_boundary,
    indicator_rows,
    select_contributions,
)


def _temporal(**zs: float) -> dict[str, dict[str, Any]]:
    return {
        k: {
            "z": z,
            "value": 0.128 * (1 + z * 0.021 / 0.128),
            "baseline_median": 0.128,
            "baseline_sigma": 0.021,
            "n_samples": 14,
            "baseline_status": "usable",
        }
        for k, z in zs.items()
    }


def _features(**kw: Any) -> Any:
    base: dict[str, Any] = dict(
        temporal=_temporal(ndti_turbidity=8.76, sediment_proxy=4.1),
        anomalous_indicators=["ndti_turbidity", "sediment_proxy"],
        affected_area_km2=2.47,
        previous_affected_area_km2=1.0,
        multivariate_score=0.66,
        rainfall_percentile=0.2,
        valid_pixel_pct=91.9,
        zone_area_km2=6.0,
        water_body_area_km2=24.0,
        z_saturation=5.0,
    )
    base.update(kw)
    return build_features(**base)


def test_feature_vector_is_normalised_and_complete() -> None:
    fv = _features()
    assert set(fv.norm) == set(FEATURE_NAMES) and set(fv.raw) == set(FEATURE_NAMES)
    assert all(0.0 <= v <= 1.0 for v in fv.norm.values())
    assert fv.norm["z_ndti_turbidity"] == 1.0  # saturates at z >= 5
    assert fv.raw["z_ndti_turbidity"] == 8.76  # signed raw kept
    assert fv.norm["n_anomalous"] == 0.8 and fv.norm["zone_area_ratio"] == 0.25
    assert fv.norm["rainfall_percentile"] == 0.0  # 20th percentile: nothing to discount
    assert _features(rainfall_percentile=0.95).norm["rainfall_percentile"] == pytest.approx(0.9)
    assert fv.raw["spatial_expansion"] == pytest.approx(1.47)
    # Clearer-than-usual water earns nothing.
    assert _features(temporal=_temporal(ndti_turbidity=-6.0)).norm["z_ndti_turbidity"] == 0.0


def test_spatial_expansion_and_rainfall_rank() -> None:
    assert spatial_expansion(None, None) is None
    assert spatial_expansion(0.5, None) == 1.0
    assert spatial_expansion(0.5, 1.0) == -0.5
    days = [date(2024, 7, 1) + timedelta(days=i) for i in range(31)]
    vals = [float(i) for i in range(31)]
    assert rainfall_percentile_rank(
        days, vals, date(2025, 7, 16), 15.0, window_days=30
    ) == pytest.approx(16 / 30)  # DOY 213 falls outside the +-15 day window
    assert rainfall_percentile_rank(days, vals, date(2025, 1, 16), 15.0, window_days=30) is None


def test_weighted_model_identity_and_documented_weights() -> None:
    m = WeightedModel()
    fv = _features()
    a = m.attribute(fv)
    assert a.model_version == "weighted-v1" and a.base == 0.0
    assert a.score == pytest.approx(sum(a.contributions.values()))
    assert a.contributions["valid_pixel_pct"] == 0.0  # cloud never drives severity
    # Two clear indicators over 40% of the zone with forest agreement reaches "high".
    assert a.score >= 70
    # Rain at the seasonal 95th percentile discounts it back to "medium".
    wet = m.attribute(_features(rainfall_percentile=0.95))
    assert wet.contributions["rainfall_percentile"] < -12 and 40 <= wet.score < 70
    # A lone spike with nothing corroborating stays "low"; forest agreement lifts it.
    lone = _features(
        temporal=_temporal(ndti_turbidity=8.0),
        anomalous_indicators=["ndti_turbidity"],
        affected_area_km2=None,
        previous_affected_area_km2=None,
        multivariate_score=None,
    )
    assert m.attribute(lone).score < 40
    lone_forest = _features(
        temporal=_temporal(ndti_turbidity=8.0),
        anomalous_indicators=["ndti_turbidity"],
        affected_area_km2=None,
        previous_affected_area_km2=None,
        multivariate_score=0.66,
    )
    assert m.attribute(lone_forest).score >= 40
    # Saturating every positive feature clips to 100 and the identity still holds.
    full = build_features(
        temporal=_temporal(ndti_turbidity=9, ndci_chlorophyll=9, fai_algal=9, sediment_proxy=9),
        anomalous_indicators=["ndti_turbidity", "ndci_chlorophyll", "fai_algal", "sediment_proxy"],
        affected_area_km2=6.0,
        previous_affected_area_km2=0.0,
        multivariate_score=0.8,
        rainfall_percentile=0.0,
        valid_pixel_pct=100,
        zone_area_km2=6.0,
        water_body_area_km2=6.0,
        z_saturation=5.0,
    )
    b = m.attribute(full)
    assert b.score == 100.0 and sum(b.contributions.values()) == pytest.approx(100.0)
    assert sum(w for w in WEIGHTS_V1.values() if w > 0) > 100  # documented: saturation clips


def test_rain_only_cannot_go_negative() -> None:
    fv = _features(
        temporal=_temporal(),
        anomalous_indicators=[],
        affected_area_km2=None,
        previous_affected_area_km2=None,
        multivariate_score=None,
        rainfall_percentile=0.99,
    )
    a = WeightedModel().attribute(fv)
    assert a.score == 0.0 and sum(a.contributions.values()) == pytest.approx(0.0, abs=1e-9)


def test_exactly_four_contributions_within_ten_percent() -> None:
    """Plan acceptance: 4 contributions summing to within 10% of the score delta."""
    m = WeightedModel()
    for fv in (
        _features(),
        _features(rainfall_percentile=0.95),
        _features(
            rainfall_percentile=0.7, multivariate_score=None, previous_affected_area_km2=None
        ),
        _features(
            temporal=_temporal(ndti_turbidity=3.2),
            anomalous_indicators=["ndti_turbidity"],
            affected_area_km2=None,
            multivariate_score=0.5,
            rainfall_percentile=None,
        ),
        _features(
            temporal=_temporal(),
            anomalous_indicators=[],
            affected_area_km2=None,
            previous_affected_area_km2=None,
            multivariate_score=None,
            rainfall_percentile=0.3,
        ),
    ):
        a = m.attribute(fv)
        cs = select_contributions(a, fv)
        assert len(cs) == 4
        delta = (a.score - a.base) / 100.0
        assert abs(sum(c.value for c in cs) - delta) <= 0.10 * abs(delta) + 1e-9
        keys = [c.key for c in cs]
        assert "rainfall_percentile" in keys  # the discount always renders
        assert {"primary_deviation", "corroboration", "spatial_extent"} <= set(keys)
        if a.contributions["rainfall_percentile"] != 0:
            assert cs[-1].key == "rainfall_percentile" and cs[-1].value < 0  # negatives last
        assert all(c.factor for c in cs)
        # Every feature's points land in exactly one factor.
        seen = [k for c in cs for k in c.parts]
        assert len(seen) == len(set(seen))
    # The example alert leads with turbidity, like the plan's mock JSON.
    top = select_contributions(m.attribute(_features()), _features())[0]
    assert top.factor == "Turbidity deviation from baseline" and top.raw == 8.76


def test_severity_bands_cap_and_confidence() -> None:
    assert severity_from_score(85, voted=True) == "high"
    assert severity_from_score(55, voted=True) == "medium"
    assert severity_from_score(12, voted=True) == "low"
    assert severity_from_score(85, voted=False) is None
    assert apply_rainfall_cap("high", True) == ("medium", "high")
    assert apply_rainfall_cap("low", True) == ("low", None)
    assert apply_rainfall_cap("high", False) == ("high", None)
    # High severity through 45% cloud is low confidence: the two never mix.
    c_cloudy, parts = confidence_from(valid_pixel_pct=45, baseline_n_samples=14, votes=1)
    c_clear, _ = confidence_from(valid_pixel_pct=98, baseline_n_samples=30, votes=3)
    assert c_cloudy < 0.6 < c_clear and set(parts) == {"observation", "baseline", "agreement"}


def test_summary_names_indicator_multiple_area_and_zone() -> None:
    """Plan acceptance: the paragraph names the indicator, the multiple, the area, the zone."""
    s = build_summary(
        flagged=True,
        primary_indicator="ndti_turbidity",
        primary_value=0.312,
        primary_baseline_median=0.128,
        primary_z=8.76,
        affected_area_km2=2.47,
        zone_area_km2=6.0,
        zone_name="Eastern zone",
        corroborating=["ndti_turbidity", "sediment_proxy"],
        multivariate_flagged=False,
        spatial_flagged=True,
        natural_cause_likely=False,
        rainfall_72h=4.2,
        rainfall_p90=40.0,
        baseline_status="usable",
    )
    assert s.startswith("Flagged because the turbidity indicator is 2.4x its seasonal baseline")
    assert "2.47 km2 of Eastern zone" in s
    assert "a correlated rise in suspended sediment" in s and "distinct patch" in s
    check_boundary(s)
    gated = build_summary(
        flagged=True,
        primary_indicator="ndti_turbidity",
        primary_value=0.312,
        primary_baseline_median=0.128,
        primary_z=8.76,
        affected_area_km2=None,
        zone_area_km2=6.0,
        zone_name="Eastern zone",
        corroborating=["ndti_turbidity"],
        multivariate_flagged=False,
        spatial_flagged=False,
        natural_cause_likely=True,
        rainfall_72h=95.0,
        rainfall_p90=40.0,
        baseline_status="usable",
    )
    assert "runoff" in gated and "capped" in gated and "6.00 km2 Eastern zone" in gated
    quiet = build_summary(
        flagged=False,
        primary_indicator=None,
        primary_value=None,
        primary_baseline_median=None,
        primary_z=None,
        affected_area_km2=None,
        zone_area_km2=6.0,
        zone_name="Eastern zone",
        corroborating=[],
        multivariate_flagged=False,
        spatial_flagged=False,
        natural_cause_likely=False,
        rainfall_72h=None,
        rainfall_p90=None,
        baseline_status="building",
    )
    assert "still building" in quiet
    with pytest.raises(ValueError):
        check_boundary("Pollution detected")
    assert "pollut" not in DISCLAIMER.lower()


def test_indicator_rows_match_api_shape() -> None:
    rows = indicator_rows({"ndti_turbidity": {"mean": 0.312}}, _temporal(ndti_turbidity=8.76))
    ndti = next(r for r in rows if r["key"] == "ndti_turbidity")
    assert ndti["baseline_mean"] == 0.128 and ndti["z_score"] == 8.76
    assert ndti["deviation_pct"] == pytest.approx(143.8, abs=0.1)
    assert {r["key"] for r in rows} >= {"ndci_chlorophyll", "fai_algal", "mndwi_extent"}
