"""Pure-NumPy seasonal baseline maths (S5, L7). No I/O.

A baseline is built per (zone, indicator) from the zone's historical
observations. For each day-of-year we take every observation whose DOY lies
within ``window_days`` (circularly, so late December and early January share a
window) and summarise it with robust statistics:

* centre  = median
* sigma   = 1.4826 * MAD  (median absolute deviation, scaled so it equals the
            standard deviation for normal data)
* p10/p90 = empirical percentiles

The median/MAD pair is what keeps a single historical spike - one turbid scene
after a flash flood, one cloud-shadow misclassification - from widening the
band that later scenes are judged against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

MAD_TO_SIGMA = 1.4826
DAYS_IN_YEAR = 366  # DOY 366 exists only in leap years; the circular distance handles it.


@dataclass(frozen=True)
class WindowStats:
    doy: int
    median: float | None
    sigma: float | None
    p10: float | None
    p90: float | None
    n_samples: int
    n_years: int


def day_of_year(when: datetime | date) -> int:
    return when.timetuple().tm_yday


def circular_doy_distance(doy: np.ndarray, centre: int) -> np.ndarray:
    """Shortest distance in days between each DOY and the centre, wrapping at year end."""
    d = np.abs(doy.astype(np.int32) - centre)
    return np.asarray(np.minimum(d, DAYS_IN_YEAR - d))


def robust_stats(values: np.ndarray) -> tuple[float, float, float, float]:
    """(median, MAD-sigma, p10, p90) over finite values. Caller guarantees len > 0."""
    v = values[np.isfinite(values)]
    med = float(np.median(v))
    sigma = float(MAD_TO_SIGMA * np.median(np.abs(v - med)))
    p10, p90 = (float(x) for x in np.percentile(v, [10, 90]))
    return med, sigma, p10, p90


def window_stats(
    doys: np.ndarray,
    years: np.ndarray,
    values: np.ndarray,
    centre: int,
    *,
    window_days: int,
) -> WindowStats:
    """Robust summary of the observations within ``window_days`` of ``centre``."""
    half = window_days / 2.0
    inside = circular_doy_distance(doys, centre) <= half
    inside &= np.isfinite(values)
    n = int(inside.sum())
    if n == 0:
        return WindowStats(centre, None, None, None, None, 0, 0)
    med, sigma, p10, p90 = robust_stats(values[inside])
    return WindowStats(centre, med, sigma, p10, p90, n, int(np.unique(years[inside]).size))


def build_seasonal_baseline(
    observed_at: list[datetime],
    values: list[float],
    *,
    window_days: int,
    doys: range | None = None,
) -> list[WindowStats]:
    """One WindowStats per day-of-year (1..366 by default) for a single series.

    ``observed_at``/``values`` are the zone-indicator history; NaN values are
    ignored. Returns a full year even when history is empty so the caller can
    persist "building" rows and the frontend can show where the band is missing.
    """
    doys = doys or range(1, DAYS_IN_YEAR + 1)
    if not observed_at:
        return [WindowStats(d, None, None, None, None, 0, 0) for d in doys]
    doy_arr = np.array([day_of_year(t) for t in observed_at], dtype=np.int32)
    year_arr = np.array([t.year for t in observed_at], dtype=np.int32)
    val_arr = np.asarray(values, dtype=np.float64)
    return [window_stats(doy_arr, year_arr, val_arr, d, window_days=window_days) for d in doys]


def zscore(value: float, stats: WindowStats, *, sigma_floor: float) -> float | None:
    """Robust z-score of ``value`` against a window; None when the window is empty.

    ``sigma_floor`` guards against a degenerate MAD (many identical historical
    values) turning a tiny deviation into a huge score. L8 owns the floor per
    indicator; it lives here so the definition of "sigma" stays in one place.
    """
    if stats.median is None or stats.sigma is None:
        return None
    return (value - stats.median) / max(stats.sigma, sigma_floor)
