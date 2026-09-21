"""L9 + L10 orchestration (S7): AnomalyCandidate -> feature vector -> priority
score, confidence, final severity -> four contributions + summary -> one
``candidate_scores`` row.

Severity and confidence are computed separately and never mixed:

* severity   = priority score bands (low < 40, medium 40-69, high >= 70), then
               the S6 rainfall cap; NULL when no detector voted.
* confidence = sqrt(cloud-free share) * (0.5 * baseline depth + 0.5 * detector
               agreement). A huge deviation seen through 45 % cloud is high
               severity at ~0.5 confidence, and both numbers say so.

Every score records ``model_version``. Rescoring (a new model, changed
weights) overwrites rows in place; the candidate itself is never touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import (
    AnomalyCandidate,
    AnomalyRun,
    CandidateScore,
    Scene,
    WaterBody,
    WaterMaskRecord,
    Zone,
)
from app.services.l06_indicators.registry import QUALITY_INDICATOR_KEYS
from app.services.l08_anomaly.gate import GATE_CAP, SEVERITY_ORDER
from app.services.l08_anomaly.service import rainfall_history
from app.services.l09_fusion.features import FeatureVector, build_features, rainfall_percentile_rank
from app.services.l09_fusion.models import Attribution, PriorityModel, active_model
from app.services.l10_explain.explain import (
    DISCLAIMER,
    Contribution,
    build_summary,
    check_boundary,
    indicator_rows,
    select_contributions,
)

log = logging.getLogger(__name__)

SEVERITY_LOW_BELOW = 40.0
SEVERITY_HIGH_FROM = 70.0
BASELINE_DEPTH_SATURATION = 15  # n_samples at which baseline depth counts as full confidence


@dataclass(frozen=True)
class Scored:
    candidate_id: int
    zone_id: str
    priority_score: float
    severity: str | None
    severity_capped_from: str | None
    confidence: float
    confidence_parts: dict[str, float]
    primary_indicator: str | None
    features: FeatureVector
    attribution: Attribution
    contributions: list[Contribution]
    summary: str
    indicators: list[dict[str, Any]]
    context: dict[str, Any]
    alertable: bool
    natural_cause_likely: bool


@dataclass
class ScoreRunResult:
    water_body_id: str
    scored: list[str] = field(default_factory=list)  # scene ids
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# --- pure pieces ----------------------------------------------------------------


def severity_from_score(score: float, *, voted: bool) -> str | None:
    if not voted:
        return None
    if score >= SEVERITY_HIGH_FROM:
        return "high"
    if score >= SEVERITY_LOW_BELOW:
        return "medium"
    return "low"


def apply_rainfall_cap(
    severity: str | None, natural_cause_likely: bool
) -> tuple[str | None, str | None]:
    if severity is None or not natural_cause_likely:
        return severity, None
    if SEVERITY_ORDER.index(severity) > SEVERITY_ORDER.index(GATE_CAP):
        return GATE_CAP, severity
    return severity, None


def confidence_from(
    *, valid_pixel_pct: float | None, baseline_n_samples: int | None, votes: int
) -> tuple[float, dict[str, float]]:
    obs = 0.0 if valid_pixel_pct is None else min(max(valid_pixel_pct / 100.0, 0.0), 1.0)
    depth = (
        0.0
        if baseline_n_samples is None
        else min(baseline_n_samples / BASELINE_DEPTH_SATURATION, 1.0)
    )
    agree = 0.4 + 0.2 * min(votes, 3) if votes else 0.4
    conf = round((obs**0.5) * (0.5 * depth + 0.5 * agree), 3)
    return conf, {"observation": round(obs, 3), "baseline": round(depth, 3), "agreement": agree}


def primary_indicator_of(cand: AnomalyCandidate) -> str | None:
    """The indicator the summary leads with: the flagged one furthest above its
    baseline, else the indicator of the largest plume, else the largest z."""
    zs = {k: cand.temporal.get(k, {}).get("z") for k in QUALITY_INDICATOR_KEYS}
    flagged = [k for k in cand.anomalous_indicators if zs.get(k) is not None]
    if flagged:
        return max(flagged, key=lambda k: zs[k])
    if cand.spatial:
        return max(cand.spatial, key=lambda c: c.get("area_km2", 0.0)).get("indicator")
    positive = {k: z for k, z in zs.items() if z is not None and z > 0}
    return max(positive, key=lambda k: positive[k]) if positive else None


# --- data access ----------------------------------------------------------------


def previous_affected_area(session: Session, zone_id: str, before: datetime) -> float | None:
    row = session.execute(
        select(AnomalyCandidate.affected_area_km2)
        .where(AnomalyCandidate.zone_id == zone_id, AnomalyCandidate.observed_at < before)
        .order_by(AnomalyCandidate.observed_at.desc())
        .limit(1)
    ).first()
    return None if row is None else row[0]


def scene_cloud_pct(session: Session, water_body_id: str, scene_id: str) -> float | None:
    return session.execute(
        select(WaterMaskRecord.cloud_pixel_pct).where(
            WaterMaskRecord.water_body_id == water_body_id, WaterMaskRecord.scene_id == scene_id
        )
    ).scalar_one_or_none()


# --- scoring one candidate ------------------------------------------------------


def score_candidate(
    cand: AnomalyCandidate,
    zone: Zone,
    wb: WaterBody,
    model: PriorityModel,
    *,
    previous_area_km2: float | None,
    rainfall_pct: float | None,
    rainfall_p90: float | None,
    cloud_pct: float | None,
    settings: Settings,
) -> Scored:
    valid = next(
        (
            v.get("valid_pixel_pct")
            for v in cand.indicator_values.values()
            if v.get("valid_pixel_pct") is not None
        ),
        None,
    )
    fv = build_features(
        temporal=cand.temporal,
        anomalous_indicators=list(cand.anomalous_indicators),
        affected_area_km2=cand.affected_area_km2,
        previous_affected_area_km2=previous_area_km2,
        multivariate_score=cand.multivariate_score,
        rainfall_percentile=rainfall_pct,
        valid_pixel_pct=valid,
        zone_area_km2=zone.area_km2,
        water_body_area_km2=wb.area_km2,
        z_saturation=settings.priority_z_saturation,
    )
    attribution = model.attribute(fv)
    voted = cand.votes > 0
    severity, capped_from = apply_rainfall_cap(
        severity_from_score(attribution.score, voted=voted), cand.natural_cause_likely
    )

    primary = primary_indicator_of(cand)
    t_primary = cand.temporal.get(primary, {}) if primary else {}
    confidence, parts = confidence_from(
        valid_pixel_pct=valid, baseline_n_samples=t_primary.get("n_samples"), votes=cand.votes
    )

    contributions = select_contributions(attribution, fv)
    summary = build_summary(
        flagged=voted,
        primary_indicator=primary,
        primary_value=t_primary.get("value"),
        primary_baseline_median=t_primary.get("baseline_median"),
        primary_z=t_primary.get("z"),
        affected_area_km2=cand.affected_area_km2,
        zone_area_km2=zone.area_km2,
        zone_name=zone.name,
        corroborating=list(cand.anomalous_indicators),
        multivariate_flagged=cand.multivariate_flag,
        spatial_flagged=cand.spatial_flag,
        natural_cause_likely=cand.natural_cause_likely,
        rainfall_72h=cand.rainfall_72h,
        rainfall_p90=rainfall_p90,
        baseline_status=cand.baseline_status,
    )
    check_boundary(summary)

    context = {
        "rainfall_72h_mm": cand.rainfall_72h,
        "rainfall_percentile": None if rainfall_pct is None else round(rainfall_pct, 3),
        "rainfall_p90_mm": None if rainfall_p90 is None else round(rainfall_p90, 1),
        "cloud_cover_pct": None if cloud_pct is None else round(cloud_pct, 1),
        "valid_pixel_pct": valid,
        "natural_cause_likely": cand.natural_cause_likely,
        "gate_reason": cand.rainfall_gate.get("reason"),
        "votes": cand.votes,
        "baseline_status": cand.baseline_status,
        "disclaimer": DISCLAIMER,
    }
    return Scored(
        candidate_id=cand.id,
        zone_id=zone.id,
        priority_score=round(attribution.score, 1),
        severity=severity,
        severity_capped_from=capped_from,
        confidence=confidence,
        confidence_parts=parts,
        primary_indicator=primary,
        features=fv,
        attribution=attribution,
        contributions=contributions,
        summary=summary,
        indicators=indicator_rows(cand.indicator_values, cand.temporal),
        context=context,
        alertable=cand.alertable,
        natural_cause_likely=cand.natural_cause_likely,
    )


def upsert_score(session: Session, cand: AnomalyCandidate, s: Scored) -> None:
    row = {
        "candidate_id": cand.id,
        "water_body_id": cand.water_body_id,
        "zone_id": cand.zone_id,
        "scene_id": cand.scene_id,
        "observed_at": cand.observed_at,
        "priority_score": s.priority_score,
        "severity": s.severity,
        "severity_capped_from": s.severity_capped_from,
        "confidence": s.confidence,
        "confidence_parts": s.confidence_parts,
        "primary_indicator": s.primary_indicator,
        "alertable": s.alertable,
        "natural_cause_likely": s.natural_cause_likely,
        "model_version": s.attribution.model_version,
        "model_base": s.attribution.base,
        "features": s.features.to_dict(),
        "contributions": [c.to_dict() for c in s.contributions],
        "summary": s.summary,
        "indicators": s.indicators,
        "context": s.context,
        "scored_at": datetime.now(UTC),
    }
    stmt = insert(CandidateScore).values(row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CandidateScore.candidate_id],
        set_={k: getattr(stmt.excluded, k) for k in row if k != "candidate_id"},
    )
    session.execute(stmt)


# --- orchestration --------------------------------------------------------------


def score_scene(
    session: Session,
    store: ObjectStore,
    wb: WaterBody,
    scene: Scene,
    *,
    settings: Settings | None = None,
    model: PriorityModel | None = None,
) -> list[Scored]:
    """Score every candidate of one (water body, scene). Idempotent: rows are
    overwritten, so calling twice with the same model is a no-op in effect."""
    settings = settings or get_settings()
    model = model or active_model(session, store, settings=settings)
    cands = list(
        session.scalars(
            select(AnomalyCandidate).where(
                AnomalyCandidate.water_body_id == wb.id, AnomalyCandidate.scene_id == scene.id
            )
        ).all()
    )
    if not cands:
        return []
    zones = {
        z.id: z for z in session.scalars(select(Zone).where(Zone.water_body_id == wb.id)).all()
    }
    days, vals = rainfall_history(session, wb.id)
    scene_day = scene.sensed_at.date()
    cloud = scene_cloud_pct(session, wb.id, scene.id)
    out: list[Scored] = []
    for cand in cands:
        rain_pct = rainfall_percentile_rank(
            days, vals, scene_day, cand.rainfall_72h, window_days=settings.rainfall_gate_window_days
        )
        scored = score_candidate(
            cand,
            zones[cand.zone_id],
            wb,
            model,
            previous_area_km2=previous_affected_area(session, cand.zone_id, cand.observed_at),
            rainfall_pct=rain_pct,
            rainfall_p90=cand.rainfall_gate.get("p90_doy"),
            cloud_pct=cloud,
            settings=settings,
        )
        upsert_score(session, cand, scored)
        out.append(scored)
        if scored.severity is not None:
            log.info(
                "candidate scored",
                extra={
                    "zone_id": cand.zone_id,
                    "scene_id": scene.id,
                    "priority": scored.priority_score,
                    "severity": scored.severity,
                    "confidence": scored.confidence,
                    "model": scored.attribution.model_version,
                },
            )
    return out


def scenes_needing_scores(session: Session, water_body_id: str, scene_ids: list[str]) -> list[str]:
    """Scenes with a finished L8 run but no score rows yet."""
    if not scene_ids:
        return []
    done_l8 = set(
        session.scalars(
            select(AnomalyRun.scene_id).where(
                AnomalyRun.water_body_id == water_body_id,
                AnomalyRun.scene_id.in_(scene_ids),
                AnomalyRun.status == "done",
            )
        ).all()
    )
    scored = set(
        session.scalars(
            select(CandidateScore.scene_id)
            .where(
                CandidateScore.water_body_id == water_body_id,
                CandidateScore.scene_id.in_(scene_ids),
            )
            .distinct()
        ).all()
    )
    return [s for s in scene_ids if s in done_l8 and s not in scored]


def anomaly_scenes(
    session: Session, water_body_id: str, date_from: date, date_to: date
) -> list[Scene]:
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, 23, 59, 59, tzinfo=UTC)
    stmt = (
        select(Scene)
        .join(AnomalyRun, AnomalyRun.scene_id == Scene.id)
        .where(
            AnomalyRun.water_body_id == water_body_id,
            AnomalyRun.status == "done",
            Scene.sensed_at >= start,
            Scene.sensed_at <= end,
        )
        .order_by(Scene.sensed_at)
    )
    return list(session.scalars(stmt).all())


def process_water_body(
    session: Session,
    store: ObjectStore,
    water_body_id: str,
    date_from: date,
    date_to: date | None = None,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> ScoreRunResult:
    """(Re)score every L8-processed scene in the window. ``force`` rescores
    scenes that already have rows (e.g. after activating a new model)."""
    settings = settings or get_settings()
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    model = active_model(session, store, settings=settings)
    result = ScoreRunResult(water_body_id=water_body_id)
    scenes = anomaly_scenes(session, water_body_id, date_from, date_to or date_from)
    todo = set(scenes_needing_scores(session, water_body_id, [s.id for s in scenes]))
    for scene in scenes:
        if not force and scene.id not in todo:
            result.skipped.append(scene.id)
            continue
        try:
            score_scene(session, store, wb, scene, settings=settings, model=model)
        except Exception:
            result.failed.append(scene.id)
            log.exception("scoring failed", extra={"scene_id": scene.id})
            raise
        result.scored.append(scene.id)
    return result
