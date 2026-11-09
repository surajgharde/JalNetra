"""Anomaly simulator: demo-grade anomalies without waiting for a satellite pass.

Every row it writes is stamped ``origin='simulation'`` (migration 0016). That
stamp is the *only* thing separating these from real detections -- the API does
not expose it and the frontend cannot see it -- so it is written on the scene,
the candidate, the score and the alert, and ``purge_simulations`` exists so the
whole set can be removed again in one call.

What it does NOT do is fabricate the verdict. Only the *inputs* are synthetic:
an indicator value placed a chosen number of sigma off the zone's own real
baseline, and a plume polygon drawn inside the zone. Those go through the real
``temporal_detector`` -> ``judge_zone`` -> ``score_scene`` -> ``assemble_scene``
chain, so severity, confidence, priority, the four contributions and the
summary are all computed by production code on production thresholds. A
simulated "high" is therefore high for the same reason a real one is, and if
the requested severity is not reachable the caller is told what came out
instead rather than having it forced.

Each run invents its own ``Scene`` (``sim_...``, ``source='simulation'``) so it
can never collide with a real scene's candidates -- ``anomaly_candidates`` is
unique on (zone_id, scene_id), and writing into a real scene would overwrite a
real detection.
"""

from __future__ import annotations

import logging
import math
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import (
    Alert,
    AnomalyCandidate,
    CandidateScore,
    Scene,
    WaterBody,
    Zone,
)
from app.services.l06_indicators.registry import INDICATORS, QUALITY_INDICATOR_KEYS
from app.services.l07_baseline.service import BaselineWindow, get_baseline
from app.services.l08_anomaly.multivariate import SceneFeatures
from app.services.l08_anomaly.service import judge_zone, upsert_candidate
from app.services.l08_anomaly.spatial import SpatialCluster
from app.services.l09_fusion.service import score_scene
from app.services.l11_alerts.assembler import assemble_scene

log = logging.getLogger(__name__)

SIMULATION = "simulation"
ORIGINAL = "original"

@dataclass(frozen=True)
class SeverityProfile:
    """How severe an event to synthesise, in the terms the pipeline reasons in."""

    primary_z: float  # sigma off the primary indicator's own baseline
    n_corroborators: int  # how many *other* indicators also deviate
    area_km2: float | None  # plume area, or None for no plume at all


#: Calibrated against the real alerts in this database rather than guessed, which
#: matters because L9 derives severity from the *priority score* band (low < 40,
#: medium 40-69, high >= 70), not from the detector vote. Profiling the real
#: scores showed what actually separates the bands:
#:
#:     severity  n   avg priority  avg n_anomalous  avg area ratio
#:     high      2   82.3          3.50             0.032
#:     medium   13   49.5          1.62             0.140
#:     low     216    7.9          0.24             0.119
#:
#: So a high alert is driven by *several indicators deviating at once*, not by a
#: large plume -- the real highs have smaller plumes than the mediums. Simulating
#: "high" by inflating one indicator and drawing a big blob would have produced a
#: shape no real high alert has; these profiles reproduce the real one.
SEVERITY_PROFILES: dict[str, SeverityProfile] = {
    # One indicator just past the z > 3 flag threshold, no plume.
    "low": SeverityProfile(primary_z=3.2, n_corroborators=0, area_km2=None),
    # Two indicators, modest plume. Lands ~50, next to the real medium average.
    "medium": SeverityProfile(primary_z=4.5, n_corroborators=1, area_km2=0.15),
    # Three indicators deviating, one saturating. Lands ~83, next to the real
    # high average of 82.3.
    "high": SeverityProfile(primary_z=7.5, n_corroborators=2, area_km2=0.25),
}

#: Corroborating indicators in order, per primary. Physically motivated: turbidity
#: and suspended sediment move together, and both lift the chlorophyll proxy;
#: chlorophyll and floating algae go together.
CORROBORATION_CHAIN: dict[str, tuple[str, ...]] = {
    "ndti_turbidity": ("sediment_proxy", "ndci_chlorophyll"),
    "sediment_proxy": ("ndti_turbidity", "ndci_chlorophyll"),
    "ndci_chlorophyll": ("fai_algal", "ndti_turbidity"),
    "fai_algal": ("ndci_chlorophyll", "ndti_turbidity"),
}

#: A corroborating indicator deviates less than the primary. Floored just above
#: the flag threshold by ``target_z_by_indicator``: at `medium` the raw factor
#: gives 4.5 * 0.6 = 2.7, which the temporal detector would NOT flag, so the
#: "corroborating" indicator would not have corroborated anything and
#: n_anomalous would have stayed at 1.
CORROBORATOR_Z_FACTORS: tuple[float, ...] = (0.6, 0.7)

#: How far above ``anomaly_z_threshold`` a floored corroborator sits. Just clear
#: of it: the flag is ``abs(z) > threshold``, so landing exactly on it fails.
CORROBORATOR_Z_MARGIN = 1.1


@dataclass
class SimulationResult:
    water_body_id: str
    zone_id: str
    scene_id: str
    observed_on: date
    requested_severity: str
    candidate_id: int | None = None
    alert_id: str | None = None
    severity: str | None = None
    priority_score: float | None = None
    confidence: float | None = None
    votes: int = 0
    area_km2: float | None = None
    geom_kind: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def matched_request(self) -> bool:
        return self.severity == self.requested_severity


# --- synthetic inputs -----------------------------------------------------------


def scene_id_for(wb: WaterBody, on: date) -> str:
    """Unique per run: two simulations on the same body and day must not land on
    the same scene, or the second would overwrite the first's candidates."""
    return f"sim_{on:%Y%m%d}_{wb.id.removeprefix('wb_')}_{secrets.token_hex(3)}"


def make_scene(wb: WaterBody, on: date, *, cloud_pct: float = 4.0) -> Scene:
    return Scene(
        id=scene_id_for(wb, on),
        mgrs_tile=(wb.mgrs_tiles or ["SIMULATED"])[0],
        sensed_at=datetime.combine(on, time(5, 30), tzinfo=UTC),
        platform="simulated",
        cloud_pct=cloud_pct,
        # Not a STAC item; there is nothing to fetch and nothing should try.
        stac_href="",
        source=SIMULATION,
        usable=True,
        assets={},
        origin=SIMULATION,
    )


def plume_in_zone(zone_geom: BaseGeometry, area_km2: float) -> MultiPolygon:
    """A blob of roughly ``area_km2`` placed inside the zone.

    Buffered around the zone's representative point (guaranteed interior, unlike
    a centroid on a concave reservoir arm), then clipped to the zone so the
    simulated plume is never drawn over land.
    """
    anchor = zone_geom.representative_point()
    # One degree of latitude is ~111 km; longitude shrinks with cos(lat). Work in
    # degrees directly -- these geometries are stored in EPSG:4326.
    lat_km = 111.0
    lon_km = 111.0 * max(math.cos(math.radians(anchor.y)), 0.1)
    radius_deg = math.sqrt(area_km2 / math.pi) / ((lat_km + lon_km) / 2)
    blob = Point(anchor.x, anchor.y).buffer(radius_deg, quad_segs=16)
    clipped = blob.intersection(zone_geom)
    if clipped.is_empty:
        clipped = blob
    if isinstance(clipped, Polygon):
        return MultiPolygon([clipped])
    polys = [g for g in getattr(clipped, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(polys) if polys else MultiPolygon([blob])


def _fallback_baseline(zone_id: str, key: str) -> BaselineWindow:
    """A plausible usable baseline for a zone that has none.

    Only reached when the body was never backfilled. The numbers are the middle
    of each indicator's registry range, so a simulated deviation off them is at
    least dimensionally sensible -- but it is recorded in the result's notes,
    because a simulation standing on an invented baseline is weaker evidence
    than one standing on a real seasonal band.
    """
    spec = INDICATORS[key]
    lo, hi = spec.valid_range
    mean = (lo + hi) / 2 if math.isfinite(lo) and math.isfinite(hi) else 0.1
    std = abs(hi - lo) / 20 if math.isfinite(lo) and math.isfinite(hi) else 0.02
    return BaselineWindow(
        zone_id=zone_id,
        indicator=key,
        doy_window=15,
        window_days=15,
        mean=round(mean, 4),
        std=round(max(std, 1e-3), 4),
        p10=round(mean - std, 4),
        p90=round(mean + std, 4),
        n_samples=24,
        n_years=2,
        history_days=730,
        status="usable",
    )


def target_z_by_indicator(
    profile: SeverityProfile, primary: str, *, z_threshold: float = 3.0
) -> dict[str, float]:
    """Which indicators deviate, and by how many sigma.

    Corroborators are floored just above ``z_threshold``: an indicator the
    detector does not flag contributes nothing to ``n_anomalous``, which is the
    feature the severity band actually turns on, so an unflagged "corroborator"
    would silently make a medium simulation look like a low one.
    """
    out = {primary: profile.primary_z}
    chain = CORROBORATION_CHAIN.get(primary, ())
    floor = z_threshold * CORROBORATOR_Z_MARGIN
    for i in range(min(profile.n_corroborators, len(chain), len(CORROBORATOR_Z_FACTORS))):
        out[chain[i]] = round(max(profile.primary_z * CORROBORATOR_Z_FACTORS[i], floor), 3)
    return out


def deviated_values(
    baselines: dict[str, BaselineWindow],
    targets: dict[str, float],
) -> dict[str, dict[str, float | None]]:
    """Indicator values for the zone: each indicator in ``targets`` placed that
    many sigma above its own baseline, the rest sitting on their baseline mean
    (z ~ 0, so they do not deviate and do not inflate the score).

    The value is clipped to the indicator's physical range, because a spectral
    index outside [-1, 1] is not a severe event, it is an impossible reading --
    and the pipeline would be scoring a number no sensor could produce.
    """
    values: dict[str, dict[str, float | None]] = {}
    for key, bl in baselines.items():
        mean = bl.mean if bl.mean is not None else 0.1
        # The temporal detector divides by max(std, sigma_floor), so the value has
        # to be built on the same denominator or the z it computes comes out
        # lower than the one asked for.
        std = bl.std if bl.std else 0.01
        z = targets.get(key, 0.0)
        lo, hi = INDICATORS[key].valid_range
        values[key] = {
            "mean": round(min(max(mean + z * std, lo), hi), 6),
            "p90": round(min(max(mean + (z + 0.5) * std, lo), hi), 6),
            "valid_pixel_pct": 94.0,
            "water_fraction_pct": 88.0,
        }
    return values


def _history(observed_at: datetime, baselines: dict[str, BaselineWindow]) -> list[SceneFeatures]:
    """A quiet history sitting on the baseline means.

    The multivariate detector needs ``multivariate_min_history`` scenes before
    it will fit at all; without this it abstains and can never corroborate, so
    a simulated high would be unreachable. The history is deliberately flat:
    it makes today's deviation the outlier, which is the point.
    """
    from datetime import timedelta

    out: list[SceneFeatures] = []
    for i in range(30, 0, -1):
        out.append(
            SceneFeatures(
                observed_at=observed_at - timedelta(days=5 * i),
                indicators={
                    k: (baselines[k].mean if k in baselines else None)
                    for k in QUALITY_INDICATOR_KEYS
                },
                water_fraction_pct=88.0,
                rainfall_72h=1.0,
            )
        )
    return out


# --- the simulation -------------------------------------------------------------


def simulate_anomaly(
    session: Session,
    store: ObjectStore,
    *,
    wb: WaterBody,
    zone: Zone,
    severity: str = "high",
    observed_on: date | None = None,
    indicator: str = "ndti_turbidity",
    rainfall_72h: float = 1.0,
    settings: Settings | None = None,
) -> SimulationResult:
    """Write one simulated anomaly and its alert, through the real pipeline."""
    settings = settings or get_settings()
    if severity not in SEVERITY_PROFILES:
        raise ValueError(f"severity must be one of {sorted(SEVERITY_PROFILES)}, got {severity!r}")
    if indicator not in QUALITY_INDICATOR_KEYS:
        raise ValueError(f"indicator must be one of {list(QUALITY_INDICATOR_KEYS)}")
    on = observed_on or datetime.now(UTC).date()
    result = SimulationResult(
        water_body_id=wb.id,
        zone_id=zone.id,
        scene_id="",
        observed_on=on,
        requested_severity=severity,
    )

    scene = make_scene(wb, on)
    session.add(scene)
    session.flush()
    result.scene_id = scene.id

    # Real baselines where the body has them: a deviation measured off the zone's
    # own seasonal band is what makes the simulation defensible.
    baselines: dict[str, BaselineWindow] = {}
    invented = []
    for key in INDICATORS:
        bl = get_baseline(session, zone.id, key, scene.sensed_at, settings=settings)
        if not bl.usable:
            bl = _fallback_baseline(zone.id, key)
            invented.append(key)
        baselines[key] = bl
    if invented:
        result.notes.append(
            f"no usable baseline for {', '.join(invented)}; invented one "
            "(run a backfill + build_baselines for a realistic simulation)"
        )

    profile = SEVERITY_PROFILES[severity]
    targets = target_z_by_indicator(
        profile, indicator, z_threshold=settings.anomaly_z_threshold
    )
    target_z = profile.primary_z
    area_km2 = profile.area_km2
    values = deviated_values(baselines, targets)

    # A plume, so the spatial detector has something to agree with. The low
    # profile has none: a single flagged indicator and no plume is what a real
    # low alert looks like.
    clusters: list[SpatialCluster] = []
    if area_km2 is not None:
        geom = plume_in_zone(to_shape(zone.geom), area_km2)
        clusters = [
            SpatialCluster(
                indicator=indicator,
                zone_id=zone.id,
                n_pixels=int(area_km2 * 1e4),  # 10 m pixels
                area_km2=area_km2,
                mean_z=round(target_z * 0.5, 2),
                max_z=round(target_z, 2),
                geom_4326=geom,
                zone_pixel_share=1.0,
            )
        ]

    current = SceneFeatures(
        observed_at=scene.sensed_at,
        indicators={k: values.get(k, {}).get("mean") for k in QUALITY_INDICATOR_KEYS},
        water_fraction_pct=88.0,
        rainfall_72h=rainfall_72h,
    )
    verdict = judge_zone(
        zone.id,
        values,
        baselines,
        clusters,
        _history(scene.sensed_at, baselines),
        current,
        rainfall_72h=rainfall_72h,
        # Well above today's rainfall, so the gate does not attribute the
        # deviation to runoff and cap the severity.
        rainfall_p90=max(rainfall_72h * 5, 25.0),
        rainfall_n=settings.rainfall_gate_min_history,
        settings=settings,
    )
    upsert_candidate(
        session, wb=wb, scene=scene, verdict=verdict, values=values, rainfall_72h=rainfall_72h
    )
    session.flush()

    cand = session.execute(
        select(AnomalyCandidate).where(
            AnomalyCandidate.zone_id == zone.id, AnomalyCandidate.scene_id == scene.id
        )
    ).scalar_one()
    cand.origin = SIMULATION
    result.candidate_id = cand.id
    result.votes = verdict.votes
    result.area_km2 = verdict.affected_area_km2
    result.geom_kind = verdict.geom_kind
    if verdict.suppressed_reason:
        result.notes.append(f"not alertable: {verdict.suppressed_reason}")
    session.flush()

    # Real scoring and real assembly, so priority/confidence/contributions and
    # the alert row are produced by production code, not by this module.
    scored = score_scene(session, store, wb, scene, settings=settings)
    for s in scored:
        row = session.get(CandidateScore, s.candidate_id)
        if row is not None:
            row.origin = SIMULATION
            result.severity = row.severity
            result.priority_score = row.priority_score
            result.confidence = row.confidence
    session.flush()

    assembled = assemble_scene(session, wb, scene, settings=settings)
    created = [*assembled.created, *assembled.updated]
    for alert_id in created:
        alert = session.get(Alert, alert_id)
        if alert is not None:
            alert.origin = SIMULATION
    if created:
        result.alert_id = created[0]
    else:
        result.notes.append(
            "no alert raised: the scored candidate was below "
            f"alert_min_priority ({settings.alert_min_priority})"
        )
    session.flush()

    log.info(
        "anomaly simulated",
        extra={
            "water_body_id": wb.id,
            "zone_id": zone.id,
            "scene_id": scene.id,
            "alert_id": result.alert_id,
            "requested_severity": severity,
            "severity": result.severity,
            "priority": result.priority_score,
            "origin": SIMULATION,
        },
    )
    return result


# --- finding and removing simulated rows ----------------------------------------


def count_simulations(session: Session) -> dict[str, int]:
    return {
        "scenes": _count(session, Scene),
        "anomaly_candidates": _count(session, AnomalyCandidate),
        "candidate_scores": _count(session, CandidateScore),
        "alerts": _count(session, Alert),
    }


def _count(session: Session, model: Any) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(model).where(model.origin == SIMULATION)
        ).scalar_one()
    )


def list_simulated_alerts(session: Session, limit: int = 100) -> list[Alert]:
    return list(
        session.scalars(
            select(Alert)
            .where(Alert.origin == SIMULATION)
            .order_by(Alert.last_observed_at.desc())
            .limit(limit)
        ).all()
    )


def purge_simulations(session: Session) -> dict[str, int]:
    """Delete every simulated row, returning how many of each went.

    Deleting the simulated *scenes* would take the candidates and scores with
    them -- both cascade on scene_id -- but ``alerts`` does not cascade from
    scenes, so every table is deleted explicitly and in dependency order.
    Nothing here can touch an ``original`` row: every statement filters on
    ``origin``.
    """
    removed = count_simulations(session)
    for model in (Alert, CandidateScore, AnomalyCandidate, Scene):
        session.execute(delete(model).where(model.origin == SIMULATION))
    session.flush()
    log.info("simulations purged", extra={"removed": removed})
    return removed
