"""L9 feature vector (S7). Pure functions, no I/O.

One ``FeatureVector`` per AnomalyCandidate, in two forms:

* ``raw``  - physical units, stored for audit and for XGBoost training;
* ``norm`` - every feature mapped to [0, 1] so the weighted model's
             contributions are comparable and read as "how much of this
             factor's maximum effect is present".

Direction convention: a *positive* temporal z (indicator above its seasonal
median) is what the priority model rewards. All four quality indicators rise
with the condition they proxy, and "clearer than usual" is not something a
field officer drives out for. The signed z is still stored raw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from app.services.l06_indicators.registry import QUALITY_INDICATOR_KEYS
from app.services.l07_baseline.robust import circular_doy_distance, day_of_year

FEATURE_NAMES: tuple[str, ...] = (
    "z_ndti_turbidity",
    "z_ndci_chlorophyll",
    "z_fai_algal",
    "z_sediment_proxy",
    "affected_area_ratio",  # spatial cluster area / zone area
    "n_anomalous",  # quality indicators simultaneously flagged (0-4)
    "spatial_expansion",  # growth of affected area vs the previous scene
    "iforest_score",  # IsolationForest outlier score
    "rainfall_percentile",  # mm_72h rank within the seasonal window (0-1)
    "valid_pixel_pct",  # cloud-free share of the zone
    "zone_area_ratio",  # zone area / water body area
)

# Diminishing returns: the second corroborating indicator matters most.
N_ANOMALOUS_NORM: dict[int, float] = {0: 0.0, 1: 0.5, 2: 0.8}

FACTOR_LABELS: dict[str, str] = {
    "z_ndti_turbidity": "Turbidity deviation from baseline",
    "z_ndci_chlorophyll": "Chlorophyll deviation from baseline",
    "z_fai_algal": "Floating algae deviation from baseline",
    "z_sediment_proxy": "Suspended sediment deviation from baseline",
    "affected_area_ratio": "Spatial extent of affected pixels",
    "n_anomalous": "Number of indicators deviating together",
    "spatial_expansion": "Growth of the affected area since the previous pass",
    "iforest_score": "Unusual combination of indicators for this zone",
    "rainfall_percentile": "Rainfall in preceding 72h",
    "valid_pixel_pct": "Cloud-free share of the observation",
    "zone_area_ratio": "Share of the water body this zone covers",
}


@dataclass(frozen=True)
class FeatureVector:
    raw: dict[str, float | None]
    norm: dict[str, float]

    def as_array(self) -> np.ndarray:
        return np.array([self.norm[k] for k in FEATURE_NAMES], dtype=np.float64)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": {k: (None if v is None else round(v, 5)) for k, v in self.raw.items()},
            "norm": {k: round(v, 5) for k, v in self.norm.items()},
        }


def _clip01(x: float) -> float:
    return float(min(max(x, 0.0), 1.0))


def rainfall_percentile_rank(
    days: list[date], values: list[float], when: date, value: float | None, *, window_days: int
) -> float | None:
    """Fraction of the seasonal-window history at or below ``value``."""
    if value is None or not days:
        return None
    doys = np.array([day_of_year(d) for d in days], dtype=np.int32)
    vals = np.asarray(values, dtype=np.float64)
    inside = (circular_doy_distance(doys, day_of_year(when)) <= window_days / 2.0) & np.isfinite(
        vals
    )
    if not inside.any():
        return None
    return float((vals[inside] <= value).mean())


def spatial_expansion(area_now: float | None, area_prev: float | None) -> float | None:
    """Relative growth of the affected area vs the previous scene of the zone.
    None when neither scene had a cluster; a cluster appearing from nothing is +1."""
    now = area_now or 0.0
    prev = area_prev or 0.0
    if now == 0.0 and prev == 0.0:
        return None
    if prev == 0.0:
        return 1.0
    return float((now - prev) / prev)


def build_features(
    *,
    temporal: dict[str, dict[str, Any]],  # AnomalyCandidate.temporal
    anomalous_indicators: list[str],
    affected_area_km2: float | None,
    previous_affected_area_km2: float | None,
    multivariate_score: float | None,
    rainfall_percentile: float | None,
    valid_pixel_pct: float | None,
    zone_area_km2: float,
    water_body_area_km2: float,
    z_saturation: float,
) -> FeatureVector:
    raw: dict[str, float | None] = {}
    norm: dict[str, float] = {}

    for key in QUALITY_INDICATOR_KEYS:
        name = f"z_{key}"
        z = temporal.get(key, {}).get("z")
        raw[name] = None if z is None else float(z)
        norm[name] = 0.0 if z is None else _clip01(float(z) / z_saturation)

    ratio = None if affected_area_km2 is None else affected_area_km2 / max(zone_area_km2, 1e-6)
    raw["affected_area_ratio"] = ratio
    # sqrt: a plume over 10 % of a zone is already a lot; 100 % is not ten times worse.
    norm["affected_area_ratio"] = 0.0 if ratio is None else _clip01(ratio) ** 0.5

    n_anom = len([k for k in anomalous_indicators if k in QUALITY_INDICATOR_KEYS])
    raw["n_anomalous"] = float(n_anom)
    norm["n_anomalous"] = N_ANOMALOUS_NORM.get(n_anom, 1.0)

    growth = spatial_expansion(affected_area_km2, previous_affected_area_km2)
    raw["spatial_expansion"] = growth
    norm["spatial_expansion"] = 0.0 if growth is None else _clip01(growth)

    raw["iforest_score"] = multivariate_score
    # sklearn's negated score_samples sits near 0.45 for ordinary points and
    # climbs toward ~0.75 for clear outliers.
    norm["iforest_score"] = (
        0.0 if multivariate_score is None else _clip01((multivariate_score - 0.45) / 0.25)
    )

    raw["rainfall_percentile"] = rainfall_percentile
    # Only unusually wet conditions discount: dry-to-median rain explains nothing.
    norm["rainfall_percentile"] = (
        0.0 if rainfall_percentile is None else _clip01((rainfall_percentile - 0.5) / 0.5)
    )

    raw["valid_pixel_pct"] = valid_pixel_pct
    norm["valid_pixel_pct"] = 0.0 if valid_pixel_pct is None else _clip01(valid_pixel_pct / 100.0)

    zr = zone_area_km2 / max(water_body_area_km2, 1e-6)
    raw["zone_area_ratio"] = zr
    norm["zone_area_ratio"] = _clip01(zr)

    return FeatureVector(raw=raw, norm=norm)
