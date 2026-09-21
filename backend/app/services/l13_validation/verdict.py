"""Verdict computation (S11, L13). Pure functions, no I/O.

A field / lab result is judged against the indicator the alert was raised on:

* ``matched``      - the laboratory measurement that corresponds to the flagged
                     indicator is above its "elevated" threshold;
* ``not_matched``  - it was measured and is within the normal range;
* ``inconclusive`` - the sample was taken more than ``max_lag_days`` after the
                     satellite observation (the water has moved on), or the
                     corresponding measurement is missing.

Thresholds are configurable (``validation_thresholds``); the defaults are
conservative field-screening values, not regulatory limits: turbidity 10 NTU,
TSS 30 mg/L, chlorophyll-a 20 ug/L (eutrophic range), and for floating algae
the same chlorophyll figure. A validation never says "pollution": it records
whether the satellite-observed deviation was corroborated on the ground.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

# indicator -> ordered list of (lab field, "elevated when value >= threshold key")
INDICATOR_LAB_FIELDS: dict[str, tuple[str, ...]] = {
    "ndti_turbidity": ("turbidity_ntu", "tss_mg_l"),
    "sediment_proxy": ("tss_mg_l", "turbidity_ntu"),
    "ndci_chlorophyll": ("chlorophyll_ug_l",),
    "fai_algal": ("chlorophyll_ug_l",),
    "mndwi_extent": (),
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "turbidity_ntu": 10.0,
    "tss_mg_l": 30.0,
    "chlorophyll_ug_l": 20.0,
}

FIELD_LABELS: dict[str, str] = {
    "turbidity_ntu": "turbidity",
    "tss_mg_l": "total suspended solids",
    "chlorophyll_ug_l": "chlorophyll-a",
}
FIELD_UNITS: dict[str, str] = {
    "turbidity_ntu": "NTU",
    "tss_mg_l": "mg/L",
    "chlorophyll_ug_l": "ug/L",
}


@dataclass(frozen=True)
class VerdictResult:
    verdict: str  # matched | not_matched | inconclusive
    reason: str
    field: str | None = None
    value: float | None = None
    threshold: float | None = None
    lag_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "field": self.field,
            "value": self.value,
            "threshold": self.threshold,
            "lag_days": self.lag_days,
        }


def compute_verdict(
    *,
    primary_indicator: str,
    observed_on: date,
    sampled_on: date,
    lab_results: dict[str, Any],
    thresholds: dict[str, float] | None = None,
    max_lag_days: int = 5,
) -> VerdictResult:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    lag = (sampled_on - observed_on).days
    if lag > max_lag_days:
        return VerdictResult(
            "inconclusive",
            f"sampled {lag} days after the satellite observation; more than {max_lag_days} days",
            lag_days=lag,
        )
    if lag < -max_lag_days:
        return VerdictResult(
            "inconclusive",
            f"sampled {-lag} days before the satellite observation",
            lag_days=lag,
        )
    fields = INDICATOR_LAB_FIELDS.get(primary_indicator, ())
    if not fields:
        return VerdictResult(
            "inconclusive",
            f"no laboratory measurement corresponds to {primary_indicator}",
            lag_days=lag,
        )
    for field in fields:
        raw = lab_results.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        threshold = thresholds[field]
        label, unit = FIELD_LABELS[field], FIELD_UNITS[field]
        if value >= threshold:
            return VerdictResult(
                "matched",
                f"{label} {value:g} {unit} is at or above the elevated threshold "
                f"of {threshold:g} {unit}",
                field,
                value,
                threshold,
                lag,
            )
        return VerdictResult(
            "not_matched",
            f"{label} {value:g} {unit} is within the normal range (below {threshold:g} {unit})",
            field,
            value,
            threshold,
            lag,
        )
    wanted = " or ".join(FIELD_LABELS[f] for f in fields)
    return VerdictResult(
        "inconclusive", f"key measurement missing: {wanted} not reported", lag_days=lag
    )


def status_for_verdict(verdict: str) -> str:
    """Alert workflow transition on submit: a corroborated deviation is
    ``validated``, a normal reading closes it as ``dismissed`` (a false positive
    worth learning from), and an inconclusive sample keeps it ``investigating``."""
    return {"matched": "validated", "not_matched": "dismissed"}.get(verdict, "investigating")
