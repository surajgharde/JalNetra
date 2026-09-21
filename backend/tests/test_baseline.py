"""S5 pure-function tests: robust DOY windows, rainfall rolling sums, backfill plan."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np

from app.core.config import Settings
from app.services.l07_baseline.backfill import S2_L2A_ARCHIVE_START, plan_backfill
from app.services.l07_baseline.rainfall import (
    SOURCE_ARCHIVE,
    DailyPrecip,
    _parse_daily,
    rolling_72h,
)
from app.services.l07_baseline.robust import (
    WindowStats,
    build_seasonal_baseline,
    circular_doy_distance,
    robust_stats,
    zscore,
)
from app.services.l07_baseline.service import (
    STATUS_BUILDING,
    STATUS_USABLE,
    outlier_sensitivity,
    window_status,
)


def _two_year_series() -> tuple[list[datetime], list[float]]:
    rng = np.random.default_rng(0)
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    ts = [t0 + timedelta(days=5 * i) for i in range(146)]
    doy = np.array([t.timetuple().tm_yday for t in ts])
    vals = 0.1 + 0.25 * np.exp(-(((doy - 230) / 40) ** 2)) + rng.normal(0, 0.02, len(ts))
    return ts, [float(v) for v in vals]


def test_band_follows_monsoon_cycle() -> None:
    ts, vals = _two_year_series()
    year = build_seasonal_baseline(ts, vals, window_days=30)
    assert len(year) == 366
    jan, aug = year[14], year[229]
    assert aug.median is not None and jan.median is not None
    assert aug.median > jan.median + 0.2
    assert jan.n_years == aug.n_years == 2
    assert all(w.n_samples >= 5 for w in year)


def test_single_outlier_moves_centre_under_5pct() -> None:
    ts, vals = _two_year_series()
    spiked = list(vals)
    spiked[50] = 2.0
    before = build_seasonal_baseline(ts, vals, window_days=30)
    after = build_seasonal_baseline(ts, spiked, window_days=30)
    d = ts[50].timetuple().tm_yday
    b, a = before[d - 1], after[d - 1]
    assert b.median is not None and a.median is not None
    assert abs(a.median - b.median) / b.median < 0.05
    assert outlier_sensitivity(vals, 2.0) < 0.05
    # The MAD sigma barely moves either, unlike a raw std which would explode.
    assert b.sigma is not None and a.sigma is not None
    assert a.sigma < b.sigma * 1.5


def test_windows_wrap_around_new_year() -> None:
    assert circular_doy_distance(np.array([5, 360, 183]), 360).tolist() == [11, 0, 177]
    ts = [datetime(2024, 12, 28, tzinfo=UTC), datetime(2025, 1, 3, tzinfo=UTC)]
    year = build_seasonal_baseline(ts, [1.0, 3.0], window_days=30)
    assert year[0].n_samples == 2 and year[364].n_samples == 2


def test_robust_stats_and_zscore() -> None:
    med, sigma, p10, p90 = robust_stats(np.array([1, 2, 3, 4, 100.0]))
    assert med == 3.0 and abs(sigma - 1.4826) < 1e-9 and p10 < p90
    stats = WindowStats(1, 0.2, 0.0, None, None, 10, 2)
    z = zscore(0.25, stats, sigma_floor=0.05)  # floor guards a degenerate MAD
    assert z is not None and abs(z - 1.0) < 1e-9
    assert zscore(0.25, WindowStats(1, None, None, None, None, 0, 0), sigma_floor=0.05) is None


def test_window_status_rules() -> None:
    s = Settings(baseline_min_samples=5, baseline_min_history_days_tier1=730)
    thin = WindowStats(1, 0.1, 0.01, 0.05, 0.15, 4, 1)
    ok = WindowStats(1, 0.1, 0.01, 0.05, 0.15, 12, 2)
    assert window_status(thin, 800, tier=1, settings=s)[0] == STATUS_BUILDING
    assert window_status(ok, 400, tier=1, settings=s)[0] == STATUS_BUILDING
    assert window_status(ok, 400, tier=2, settings=s)[0] == STATUS_USABLE
    assert window_status(ok, 800, tier=1, settings=s) == (STATUS_USABLE, None)


def test_rolling_72h_uses_prior_rows_and_flags_gaps() -> None:
    d0 = date(2026, 6, 10)
    days = [DailyPrecip(d0 + timedelta(days=i), float(i + 1), SOURCE_ARCHIVE) for i in range(3)]
    prior = {d0 - timedelta(days=1): 10.0, d0 - timedelta(days=2): 20.0}
    out = rolling_72h(days, prior)
    assert out[d0] == 31.0 and out[d0 + timedelta(days=2)] == 6.0
    assert rolling_72h(days, {})[d0] is None


def test_parse_daily_skips_unassimilated_days() -> None:
    payload = {"daily": {"time": ["2026-06-10", "2026-06-11"], "precipitation_sum": [4.2, None]}}
    rows = _parse_daily(payload, SOURCE_ARCHIVE)
    assert rows == [DailyPrecip(date(2026, 6, 10), 4.2, SOURCE_ARCHIVE)]


def test_backfill_plan_chunks_and_archive_floor() -> None:
    s = Settings(baseline_history_years=3, baseline_backfill_chunk_days=31)
    plan = plan_backfill("wb_x", settings=s, today=date(2026, 9, 21))
    assert plan.date_from == date(2023, 9, 22) and plan.chunks[0][0] == plan.date_from
    assert plan.chunks[-1][1] == date(2026, 9, 21)
    assert all((e - st).days <= 30 for st, e in plan.chunks)
    assert (
        sum((e - st).days + 1 for st, e in plan.chunks) == (plan.date_to - plan.date_from).days + 1
    )
    old = plan_backfill("wb_x", date_from=date(2015, 1, 1), settings=s, today=date(2026, 9, 21))
    assert old.date_from == S2_L2A_ARCHIVE_START
