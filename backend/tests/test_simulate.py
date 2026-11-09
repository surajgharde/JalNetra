"""Anomaly simulator tests (pure pieces only; the DB chain is exercised by the
CLI against the live stack).

The point of these is the *boundary*: the simulator may synthesise inputs, but
it must not fabricate a verdict, invent an impossible reading, or leave a row
unstamped.
"""

from __future__ import annotations

import math

from shapely.geometry import MultiPolygon, Polygon

from app.services.l06_indicators.registry import INDICATORS, QUALITY_INDICATOR_KEYS
from app.services.l07_baseline.service import BaselineWindow
from app.services.l08_anomaly.temporal import temporal_detector
from app.services.ops.simulate import (
    CORROBORATION_CHAIN,
    ORIGINAL,
    SEVERITY_PROFILES,
    SIMULATION,
    deviated_values,
    plume_in_zone,
    scene_id_for,
    target_z_by_indicator,
)


def _baseline(key: str, mean: float, std: float) -> BaselineWindow:
    return BaselineWindow(
        zone_id="z",
        indicator=key,
        doy_window=300,
        window_days=15,
        mean=mean,
        std=std,
        p10=mean - std,
        p90=mean + std,
        n_samples=24,
        n_years=2,
        history_days=730,
        status="usable",
    )


def _baselines(std: float = 0.08) -> dict[str, BaselineWindow]:
    return {k: _baseline(k, 0.1, std) for k in INDICATORS}


def test_simulated_values_reproduce_the_requested_z_through_the_real_detector() -> None:
    """The simulator asks for N sigma; the real temporal detector has to agree.

    It builds the value as mean + z*std, but `score_indicator` divides by
    max(std, sigma_floor) -- so if the two disagree the z that comes out is not
    the one requested and the severity lands in the wrong band.
    """
    floors = {k: 0.02 for k in INDICATORS}
    bls = _baselines(std=0.08)  # above every floor
    targets = target_z_by_indicator(SEVERITY_PROFILES["high"], "ndti_turbidity")
    values = deviated_values(bls, targets)
    res = temporal_detector(
        {k: v["mean"] for k, v in values.items()},
        bls,
        z_threshold=3.0,
        sigma_floors=floors,
    )
    for key, want in targets.items():
        got = res.scores[key].z
        assert got is not None and math.isclose(got, want, rel_tol=0.02), (
            f"{key}: asked for z={want}, detector computed {got}"
        )
    # Only the targeted indicators deviate; the rest must sit on their baseline.
    assert set(res.anomalous_indicators) == set(targets)


def test_severity_profiles_escalate_on_breadth_not_just_magnitude() -> None:
    """Real high alerts here average 3.5 deviating indicators and a *smaller*
    plume than the mediums. A profile that escalated by inflating one indicator
    and drawing a bigger blob would not look like any real high alert."""
    low, med, high = (SEVERITY_PROFILES[s] for s in ("low", "medium", "high"))
    assert low.n_corroborators < med.n_corroborators < high.n_corroborators
    assert low.primary_z < med.primary_z < high.primary_z
    assert low.area_km2 is None, "a low alert should have no plume"
    # Breadth, not area, is what separates medium from high.
    assert high.area_km2 is not None and med.area_km2 is not None
    assert high.area_km2 < 2 * med.area_km2

    # Every corroborator still has to clear the z > 3 flag threshold, or it
    # would not count as a deviating indicator at all.
    for name, profile in SEVERITY_PROFILES.items():
        for key, z in target_z_by_indicator(profile, "ndti_turbidity").items():
            assert z > 3.0, f"{name}: {key} at z={z} would not be flagged"


def test_values_stay_inside_the_indicator_physical_range() -> None:
    """A near-constant baseline plus a 7.5-sigma push must not produce a
    spectral index outside [-1, 1]: that is not a severe event, it is a reading
    no sensor could return, and the pipeline would score it anyway."""
    bls = {k: _baseline(k, 0.9, 0.5) for k in INDICATORS}  # absurdly wide
    targets = target_z_by_indicator(SEVERITY_PROFILES["high"], "ndti_turbidity")
    values = deviated_values(bls, targets)
    for key, v in values.items():
        lo, hi = INDICATORS[key].valid_range
        for field in ("mean", "p90"):
            assert v[field] is not None
            assert lo <= float(v[field]) <= hi, f"{key}.{field}={v[field]} outside [{lo}, {hi}]"


def test_plume_is_the_requested_size_and_stays_inside_the_zone() -> None:
    zone = Polygon([(73.70, 18.40), (73.76, 18.40), (73.76, 18.44), (73.70, 18.44)])
    for area in (0.06, 0.25, 0.45):
        plume = plume_in_zone(zone, area)
        assert isinstance(plume, MultiPolygon) and not plume.is_empty
        assert zone.buffer(1e-9).contains(plume), "plume escaped the zone onto land"
        # Degrees -> km2 at this latitude; generous tolerance, it is a demo blob.
        lon_km = 111.0 * math.cos(math.radians(18.42))
        got = plume.area * 111.0 * lon_km
        assert 0.4 * area <= got <= 1.8 * area, f"asked {area} km2, drew {got:.3f}"


def test_plume_survives_a_zone_it_cannot_fit_inside() -> None:
    """A sliver zone smaller than the requested plume must still yield a usable
    polygon rather than an empty geometry the assembler would store as NULL."""
    sliver = Polygon([(73.70, 18.40), (73.7005, 18.40), (73.7005, 18.4005), (73.70, 18.4005)])
    plume = plume_in_zone(sliver, 0.45)
    assert not plume.is_empty and plume.area > 0


def test_scene_ids_are_unique_per_run_and_marked_simulated() -> None:
    """Two simulations on the same body and day must not share a scene:
    `anomaly_candidates` is unique on (zone_id, scene_id), so the second would
    silently overwrite the first."""

    class _WB:
        id = "wb_khadakwasla"

    from datetime import date

    on = date(2026, 10, 31)
    ids = {scene_id_for(_WB(), on) for _ in range(50)}  # type: ignore[arg-type]
    assert len(ids) == 50
    assert all(i.startswith("sim_20261031_khadakwasla_") for i in ids)


def test_corroboration_chains_are_real_indicators_and_exclude_the_primary() -> None:
    for primary, chain in CORROBORATION_CHAIN.items():
        assert primary in QUALITY_INDICATOR_KEYS
        assert primary not in chain, f"{primary} corroborates itself"
        assert len(set(chain)) == len(chain), f"{primary} has a duplicate corroborator"
        for key in chain:
            assert key in QUALITY_INDICATOR_KEYS, f"{primary} -> unknown indicator {key}"


def test_origin_values_are_the_two_the_migration_defines() -> None:
    """The stamp is the only thing separating simulated rows from real ones, and
    the purge filters on exactly these strings."""
    assert SIMULATION == "simulation"
    assert ORIGINAL == "original"
