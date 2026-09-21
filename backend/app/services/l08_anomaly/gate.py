"""RAINFALL GATE (S6, L8) - mandatory, pure.

Runoff after heavy rain raises turbidity and suspended sediment; that is the
catchment behaving normally, not an anomaly. When the trailing 72 h rainfall is
above the water body's historical 90th percentile for this time of year *and*
the only anomalous indicators are turbidity / sediment, the candidate is marked
``natural_cause_likely`` and its severity is capped at "medium".

Chlorophyll (NDCI) or floating algae (FAI) anomalies are never gated: rain does
not grow algae in 72 hours, and a bloom under rain is still a bloom.

Every decision is returned as a dict so the explanation panel and the PDF can
show the reader exactly why a spike was downgraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from app.services.l07_baseline.robust import circular_doy_distance, day_of_year

RUNOFF_INDICATORS = frozenset({"ndti_turbidity", "sediment_proxy"})
SEVERITY_ORDER = ("low", "medium", "high")
GATE_CAP = "medium"


@dataclass(frozen=True)
class RainfallGateDecision:
    applied: bool  # gate fired: natural_cause_likely = True
    rainfall_72h: float | None
    p90_doy: float | None
    exceeded: bool | None  # None when rainfall or history is missing
    anomalous_indicators: list[str]
    n_history: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rainfall_72h": self.rainfall_72h,
            "p90_doy": None if self.p90_doy is None else round(self.p90_doy, 2),
            "exceeded": self.exceeded,
            "anomalous_indicators": list(self.anomalous_indicators),
            "n_history": self.n_history,
            "reason": self.reason,
        }


def doy_percentile(
    days: list[date], values: list[float], when: date, *, window_days: int, q: float = 90.0
) -> tuple[float | None, int]:
    """Percentile of ``values`` whose day falls within the DOY window around ``when``.
    Returns (percentile, n_in_window)."""
    if not days:
        return None, 0
    doys = np.array([day_of_year(d) for d in days], dtype=np.int32)
    vals = np.asarray(values, dtype=np.float64)
    inside = circular_doy_distance(doys, day_of_year(when)) <= window_days / 2.0
    inside &= np.isfinite(vals)
    n = int(inside.sum())
    if n == 0:
        return None, 0
    return float(np.percentile(vals[inside], q)), n


def rainfall_gate(
    anomalous_indicators: list[str],
    rainfall_72h: float | None,
    p90_doy: float | None,
    *,
    n_history: int,
    min_history: int,
) -> RainfallGateDecision:
    inds = list(anomalous_indicators)
    if not inds:
        return RainfallGateDecision(
            False, rainfall_72h, p90_doy, None, inds, n_history, "no anomalous indicator"
        )
    if rainfall_72h is None:
        return RainfallGateDecision(
            False, None, p90_doy, None, inds, n_history, "no rainfall row for the scene date"
        )
    if p90_doy is None or n_history < min_history:
        return RainfallGateDecision(
            False,
            rainfall_72h,
            p90_doy,
            None,
            inds,
            n_history,
            f"rainfall history too thin for a seasonal p90 ({n_history} < {min_history} days)",
        )
    exceeded = rainfall_72h > p90_doy
    runoff_only = set(inds) <= RUNOFF_INDICATORS
    if exceeded and runoff_only:
        return RainfallGateDecision(
            True,
            rainfall_72h,
            p90_doy,
            True,
            inds,
            n_history,
            f"72 h rainfall {rainfall_72h:.1f} mm exceeds the seasonal p90 of {p90_doy:.1f} mm "
            f"and only runoff-driven indicators ({', '.join(inds)}) deviate; "
            "runoff is the likely cause",
        )
    if exceeded:
        others = sorted(set(inds) - RUNOFF_INDICATORS)
        return RainfallGateDecision(
            False,
            rainfall_72h,
            p90_doy,
            True,
            inds,
            n_history,
            f"72 h rainfall {rainfall_72h:.1f} mm exceeds the seasonal p90 of {p90_doy:.1f} mm "
            f"but {', '.join(others)} also deviate, which runoff does not explain",
        )
    return RainfallGateDecision(
        False,
        rainfall_72h,
        p90_doy,
        False,
        inds,
        n_history,
        f"72 h rainfall {rainfall_72h:.1f} mm is within the seasonal p90 of {p90_doy:.1f} mm",
    )


def cap_severity(
    severity: str | None, decision: RainfallGateDecision
) -> tuple[str | None, str | None]:
    """Apply the gate's cap. Returns (severity, capped_from)."""
    if severity is None or not decision.applied:
        return severity, None
    if SEVERITY_ORDER.index(severity) > SEVERITY_ORDER.index(GATE_CAP):
        return GATE_CAP, severity
    return severity, None
