"""Detector 1 - TEMPORAL (S6, L8). Pure functions, no I/O.

Robust z of the current zone-indicator value against its day-of-year baseline:

    z = (x - median) / max(1.4826 * MAD, sigma_floor)

An indicator is flagged when |z| exceeds the threshold *and* its baseline is
usable. A building baseline yields ``z=None, flagged=False`` with the reason
carried through, so the UI can say "baseline building" rather than "normal".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.l06_indicators.registry import QUALITY_INDICATOR_KEYS
from app.services.l07_baseline.robust import WindowStats, zscore
from app.services.l07_baseline.service import BaselineWindow


@dataclass(frozen=True)
class TemporalScore:
    indicator: str
    value: float | None
    z: float | None
    flagged: bool
    direction: str | None  # high | low | None
    baseline_status: str  # usable | building
    baseline_median: float | None
    baseline_sigma: float | None
    n_samples: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "z": None if self.z is None else round(self.z, 3),
            "flagged": self.flagged,
            "direction": self.direction,
            "baseline_status": self.baseline_status,
            "baseline_median": self.baseline_median,
            "baseline_sigma": self.baseline_sigma,
            "n_samples": self.n_samples,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TemporalResult:
    scores: dict[str, TemporalScore]
    flagged: bool
    anomalous_indicators: list[str]  # quality indicators only, |z| > threshold
    max_abs_z: float | None
    baseline_status: str  # usable if any quality indicator has a usable baseline

    def to_dict(self) -> dict[str, Any]:
        return {k: s.to_dict() for k, s in self.scores.items()}


def score_indicator(
    indicator: str,
    value: float | None,
    baseline: BaselineWindow,
    *,
    z_threshold: float,
    sigma_floor: float,
) -> TemporalScore:
    if value is None:
        return TemporalScore(
            indicator,
            None,
            None,
            False,
            None,
            baseline.status,
            baseline.mean,
            baseline.std,
            baseline.n_samples,
            "no observation for this zone-indicator",
        )
    if not baseline.usable:
        return TemporalScore(
            indicator,
            value,
            None,
            False,
            None,
            baseline.status,
            baseline.mean,
            baseline.std,
            baseline.n_samples,
            baseline.reason or "baseline building",
        )
    stats = WindowStats(
        baseline.doy_window,
        baseline.mean,
        baseline.std,
        baseline.p10,
        baseline.p90,
        baseline.n_samples,
        baseline.n_years,
    )
    z = zscore(value, stats, sigma_floor=sigma_floor)
    if z is None:
        return TemporalScore(
            indicator,
            value,
            None,
            False,
            None,
            "building",
            baseline.mean,
            baseline.std,
            baseline.n_samples,
            "baseline window has no centre",
        )
    flagged = abs(z) > z_threshold
    direction = "high" if z > 0 else "low"
    return TemporalScore(
        indicator,
        value,
        z,
        flagged,
        direction if flagged else None,
        baseline.status,
        baseline.mean,
        baseline.std,
        baseline.n_samples,
    )


def temporal_detector(
    values: dict[str, float | None],
    baselines: dict[str, BaselineWindow],
    *,
    z_threshold: float,
    sigma_floors: dict[str, float],
    default_floor: float = 0.01,
) -> TemporalResult:
    """Score every indicator present in ``baselines``; vote on the quality ones."""
    scores: dict[str, TemporalScore] = {}
    for key, baseline in baselines.items():
        scores[key] = score_indicator(
            key,
            values.get(key),
            baseline,
            z_threshold=z_threshold,
            sigma_floor=sigma_floors.get(key, default_floor),
        )
    quality = [scores[k] for k in QUALITY_INDICATOR_KEYS if k in scores]
    anomalous = [s.indicator for s in quality if s.flagged]
    zs = [abs(s.z) for s in quality if s.z is not None]
    usable = any(s.baseline_status == "usable" for s in quality)
    return TemporalResult(
        scores=scores,
        flagged=bool(anomalous),
        anomalous_indicators=anomalous,
        max_abs_z=max(zs) if zs else None,
        baseline_status="usable" if usable else "building",
    )
