"""Detector 3 - MULTIVARIATE (S6, L8). scikit-learn IsolationForest, no I/O.

Feature vector per scene for one zone:

    [ndti, ndci, fai, sediment, water_extent_delta, rainfall_72h]

fitted on that zone's own history (current scene excluded) with
contamination=0.05. The forest catches combinations no single z sees, e.g.
turbidity mildly up, chlorophyll mildly up, extent unchanged and *no rain* -
each ordinary alone, together unusual for this zone.

``water_extent_delta`` is the zone's water fraction minus the median water
fraction of its history within the same DOY window, so seasonal draw-down is
not read as change. ``rainfall_72h`` is included so the forest learns that
turbidity-with-rain is normal; a missing rainfall day is imputed with the
history median and recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from sklearn.ensemble import IsolationForest

from app.services.l07_baseline.robust import circular_doy_distance, day_of_year

FEATURES: tuple[str, ...] = (
    "ndti_turbidity",
    "ndci_chlorophyll",
    "fai_algal",
    "sediment_proxy",
    "water_extent_delta",
    "rainfall_72h",
)
INDICATOR_FEATURES: tuple[str, ...] = FEATURES[:4]


@dataclass(frozen=True)
class SceneFeatures:
    observed_at: datetime
    indicators: dict[str, float | None]  # the four quality indicators (zone mean)
    water_fraction_pct: float | None
    rainfall_72h: float | None


@dataclass(frozen=True)
class MultivariateResult:
    fitted: bool
    score: float | None  # higher = more unusual (negated sklearn score_samples)
    flagged: bool
    n_history: int
    features: dict[str, float | None]
    imputed: list[str]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fitted": self.fitted,
            "score": None if self.score is None else round(self.score, 4),
            "flagged": self.flagged,
            "n_history": self.n_history,
            "features": {k: (None if v is None else round(v, 5)) for k, v in self.features.items()},
            "imputed": list(self.imputed),
            "reason": self.reason,
        }


def water_extent_delta(
    history: list[SceneFeatures], current: SceneFeatures, *, window_days: int
) -> tuple[np.ndarray, float | None]:
    """Per-row water_fraction minus the DOY-window median (over history rows)."""
    hist_doy = np.array([day_of_year(h.observed_at) for h in history], dtype=np.int32)
    hist_wf = np.array(
        [np.nan if h.water_fraction_pct is None else h.water_fraction_pct for h in history],
        dtype=np.float64,
    )

    def median_at(doy: int) -> float:
        inside = (circular_doy_distance(hist_doy, doy) <= window_days / 2.0) & np.isfinite(hist_wf)
        return float(np.median(hist_wf[inside])) if inside.any() else float("nan")

    deltas = np.array([hist_wf[i] - median_at(int(hist_doy[i])) for i in range(len(history))])
    cur = (
        None
        if current.water_fraction_pct is None or not history
        else current.water_fraction_pct - median_at(day_of_year(current.observed_at))
    )
    if cur is not None and not np.isfinite(cur):
        cur = None
    return deltas, cur


def build_matrix(
    history: list[SceneFeatures], current: SceneFeatures, *, window_days: int
) -> tuple[np.ndarray, np.ndarray, dict[str, float | None], list[str]]:
    """(X_history, x_current, current feature dict, imputed feature names).
    NaNs are imputed column-wise with the history median."""
    deltas, cur_delta = water_extent_delta(history, current, window_days=window_days)
    rows = []
    for h, d in zip(history, deltas, strict=True):
        rows.append(
            [
                *[
                    np.nan if h.indicators.get(k) is None else h.indicators[k]
                    for k in INDICATOR_FEATURES
                ],
                d,
                np.nan if h.rainfall_72h is None else h.rainfall_72h,
            ]
        )
    x_hist = np.asarray(rows, dtype=np.float64).reshape(-1, len(FEATURES))
    x_cur = np.array(
        [
            *[
                np.nan if current.indicators.get(k) is None else current.indicators[k]
                for k in INDICATOR_FEATURES
            ],
            np.nan if cur_delta is None else cur_delta,
            np.nan if current.rainfall_72h is None else current.rainfall_72h,
        ],
        dtype=np.float64,
    )
    imputed: list[str] = []
    if len(x_hist):
        med = np.nanmedian(x_hist, axis=0)
        med = np.where(np.isfinite(med), med, 0.0)
        x_hist = np.where(np.isfinite(x_hist), x_hist, med)
        for j, name in enumerate(FEATURES):
            if not np.isfinite(x_cur[j]):
                x_cur[j] = med[j]
                imputed.append(name)
    feats = {
        name: (None if name in imputed else float(x_cur[j])) for j, name in enumerate(FEATURES)
    }
    return x_hist, x_cur, feats, imputed


def multivariate_detector(
    history: list[SceneFeatures],
    current: SceneFeatures,
    *,
    min_history: int,
    contamination: float,
    seed: int,
    window_days: int = 30,
) -> MultivariateResult:
    x_hist, x_cur, feats, imputed = build_matrix(history, current, window_days=window_days)
    n = len(x_hist)
    if n < min_history:
        return MultivariateResult(
            False,
            None,
            False,
            n,
            feats,
            imputed,
            f"{n} historical scenes; IsolationForest needs {min_history}",
        )
    missing = [k for k in INDICATOR_FEATURES if k in imputed]
    if len(missing) == len(INDICATOR_FEATURES):
        return MultivariateResult(
            False, None, False, n, feats, imputed, "no indicator observed for this zone"
        )
    forest = IsolationForest(n_estimators=200, contamination=contamination, random_state=seed).fit(
        x_hist
    )
    score = float(-forest.score_samples(x_cur.reshape(1, -1))[0])
    flagged = bool(forest.predict(x_cur.reshape(1, -1))[0] == -1)
    return MultivariateResult(True, score, flagged, n, feats, imputed)
