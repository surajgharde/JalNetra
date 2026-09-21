"""S11 tests: verdict rules, alert status transition, precision buckets,
retraining guard, and the contract inventory for the new endpoints."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from app.core.storage import MemoryStore
from app.main import app
from app.services.l09_fusion.features import FEATURE_NAMES, FeatureVector
from app.services.l09_fusion.models import LabelledExample, WeightedModel
from app.services.l13_validation.service import _bucket
from app.services.l13_validation.training import (
    feature_vector_from_score,
    precision_at_threshold,
    retrain,
)
from app.services.l13_validation.verdict import compute_verdict, status_for_verdict

OBS = date(2026, 9, 17)


def _v(indicator: str, sampled: date, **lab: float) -> Any:
    return compute_verdict(
        primary_indicator=indicator, observed_on=OBS, sampled_on=sampled, lab_results=lab
    )


def test_verdict_rules_from_plan() -> None:
    # matched: lab confirms an elevated reading for the flagged indicator
    m = _v("ndti_turbidity", date(2026, 9, 19), turbidity_ntu=48.0)
    assert m.verdict == "matched" and m.field == "turbidity_ntu" and m.lag_days == 2
    assert "48 NTU" in m.reason and "pollut" not in m.reason.lower()
    # not_matched: within normal range
    n = _v("ndti_turbidity", date(2026, 9, 18), turbidity_ntu=3.5)
    assert n.verdict == "not_matched" and n.threshold == 10.0
    # inconclusive: > 5 days after the observation, even with a high reading
    late = _v("ndti_turbidity", date(2026, 9, 23), turbidity_ntu=48.0)
    assert late.verdict == "inconclusive" and "6 days" in late.reason
    # inconclusive: key field missing (pH alone says nothing about turbidity)
    missing = _v("ndti_turbidity", date(2026, 9, 18), ph=7.4)
    assert missing.verdict == "inconclusive" and "turbidity" in missing.reason
    # sediment falls back to turbidity when TSS was not measured
    assert _v("sediment_proxy", date(2026, 9, 18), turbidity_ntu=20.0).verdict == "matched"
    # chlorophyll indicators use chlorophyll-a
    assert _v("fai_algal", date(2026, 9, 18), chlorophyll_ug_l=35.0).verdict == "matched"
    assert _v("ndci_chlorophyll", date(2026, 9, 18), chlorophyll_ug_l=5.0).verdict == "not_matched"
    # exactly 5 days is still conclusive; a sample well before the pass is not
    assert _v("ndti_turbidity", date(2026, 9, 22), turbidity_ntu=48.0).verdict == "matched"
    assert _v("ndti_turbidity", date(2026, 9, 1), turbidity_ntu=48.0).verdict == "inconclusive"


def test_verdict_thresholds_are_configurable() -> None:
    v = compute_verdict(
        primary_indicator="ndti_turbidity",
        observed_on=OBS,
        sampled_on=OBS,
        lab_results={"turbidity_ntu": 15.0},
        thresholds={"turbidity_ntu": 25.0},
    )
    assert v.verdict == "not_matched" and v.threshold == 25.0


def test_submit_updates_alert_status() -> None:
    """Plan acceptance: submitting a validation updates the alert status."""
    assert status_for_verdict("matched") == "validated"
    assert status_for_verdict("not_matched") == "dismissed"
    assert status_for_verdict("inconclusive") == "investigating"


def test_precision_buckets_move_with_verdicts() -> None:
    rows: list[tuple[str | None, str | None]] = [
        ("ndti_turbidity", "matched"),
        ("ndti_turbidity", "matched"),
        ("ndti_turbidity", "not_matched"),
        ("ndti_turbidity", "inconclusive"),
        ("fai_algal", "not_matched"),
    ]
    b = _bucket(rows)
    assert b["ndti_turbidity"]["precision"] == 0.667 and b["ndti_turbidity"]["n"] == 4
    assert b["fai_algal"]["precision"] == 0.0
    # An inconclusive-only group has no precision yet, rather than a fake 0 or 1.
    assert _bucket([("x", "inconclusive")])["x"]["precision"] is None
    # A new matched verdict moves the metric.
    b2 = _bucket([*rows, ("fai_algal", "matched")])
    assert b2["fai_algal"]["precision"] == 0.5


def _fv(z: float, rain: float = 0.0) -> FeatureVector:
    norm = dict.fromkeys(FEATURE_NAMES, 0.0)
    norm["z_ndti_turbidity"] = z
    norm["n_anomalous"] = 0.8 if z > 0.5 else 0.0
    norm["iforest_score"] = 1.0 if z > 0.5 else 0.0
    norm["rainfall_percentile"] = rain
    return FeatureVector(raw=dict.fromkeys(FEATURE_NAMES, None), norm=norm)


def test_precision_at_threshold_and_feature_roundtrip() -> None:
    model = WeightedModel()
    examples = [
        LabelledExample(_fv(1.0), 90.0),  # strong, corroborated
        LabelledExample(_fv(1.0), 10.0),  # strong, false positive
        LabelledExample(_fv(0.1), 10.0),  # weak, would not alert
    ]
    p, k = precision_at_threshold(model, examples, threshold=40.0, matched_target=90.0)
    assert k == 2 and p == 0.5
    assert precision_at_threshold(model, [examples[2]], threshold=40.0, matched_target=90.0) == (
        None,
        0,
    )
    score = SimpleNamespace(features={"norm": _fv(0.7).norm, "raw": {}})
    fv = feature_vector_from_score(score)  # type: ignore[arg-type]
    assert fv is not None and fv.norm["z_ndti_turbidity"] == 0.7
    assert feature_vector_from_score(SimpleNamespace(features={})) is None  # type: ignore[arg-type]


class _EmptySession:
    """Just enough Session for retrain() to find no validations."""

    def execute(self, *_: Any, **__: Any) -> Any:
        return SimpleNamespace(all=lambda: [])


def test_retrain_exits_cleanly_with_insufficient_data() -> None:
    """Plan acceptance: insufficient data -> logged reason, not a failure."""
    result = retrain(_EmptySession(), MemoryStore())  # type: ignore[arg-type]
    assert not result.trained and not result.promoted
    assert "needs 50" in result.reason and result.n_examples == 0
    assert result.to_dict()["version"] is None


def test_validation_endpoints_in_openapi() -> None:
    paths = app.openapi()["paths"]
    assert "post" in paths["/api/v1/validations"]
    assert "get" in paths["/api/v1/validations/summary"]
    assert "post" in paths["/api/v1/validations/{validation_id}/photo"]
    assert "get" in paths["/api/v1/validations/{validation_id}/photo"]
