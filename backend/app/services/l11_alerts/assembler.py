"""L11 alert assembler (S8): scored candidates -> ``alerts`` rows, with
deduplication.

Dedup rule (alert fatigue kills operational tools): if an open or
investigating alert exists for the same zone *and* primary indicator whose
last observation is within ``alert_dedupe_days`` of the new one, the new
observation is appended to its timeline and the alert's current fields are
refreshed; no second alert is created. The peak score/severity while open is
kept alongside the current one, and a *non-alertable* observation of the same
zone (the deviation subsided, or cloud hid it) is appended too, so the
timeline shows the whole episode rather than only the bad days.

Alert ids follow the contract: ``alr_{YYYY}_{MMDD}_{body}_{zone}``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from geoalchemy2.shape import from_shape, to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import Settings, get_settings
from app.db.models import (
    Alert,
    AnomalyCandidate,
    CandidateScore,
    IndicatorRun,
    Scene,
    WaterBody,
    WaterMaskRecord,
    Zone,
)
from app.db.models.alerts import OPEN_STATUSES
from app.services.l08_anomaly.gate import SEVERITY_ORDER
from app.services.l11_alerts.evidence import evidence_links

log = logging.getLogger(__name__)

Action = Literal["created", "updated", "appended", "skipped"]


@dataclass
class AssembleResult:
    water_body_id: str
    scene_id: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)  # alert ids refreshed by an alertable obs
    escalated: list[str] = field(default_factory=list)  # subset of updated whose severity rose
    appended: list[str] = field(default_factory=list)  # non-alertable obs added to an open alert
    skipped: int = 0


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def alert_id_for(wb: WaterBody, zone: Zone, observed_at: datetime) -> str:
    body = slug(wb.id.removeprefix("wb_"))
    return f"alr_{observed_at:%Y}_{observed_at:%m%d}_{body}_z{zone.seq}"


def severity_rank(s: str | None) -> int:
    return -1 if s is None else SEVERITY_ORDER.index(s)


def find_open_alert(
    session: Session, zone_id: str, indicator: str, observed_at: datetime, *, window_days: int
) -> Alert | None:
    """The open alert this observation belongs to, if any."""
    lo = observed_at - timedelta(days=window_days)
    hi = observed_at + timedelta(days=window_days)  # backfills can arrive out of order
    return session.execute(
        select(Alert)
        .where(
            Alert.zone_id == zone_id,
            Alert.primary_indicator == indicator,
            Alert.status.in_(OPEN_STATUSES),
            Alert.last_observed_at >= lo,
            Alert.first_observed_at <= hi,
        )
        .order_by(Alert.last_observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def find_open_alert_for_zone(
    session: Session, zone_id: str, observed_at: datetime, *, window_days: int
) -> Alert | None:
    lo = observed_at - timedelta(days=window_days)
    return session.execute(
        select(Alert)
        .where(
            Alert.zone_id == zone_id, Alert.status.in_(OPEN_STATUSES), Alert.last_observed_at >= lo
        )
        .order_by(Alert.last_observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def timeline_entry(score: CandidateScore, cand: AnomalyCandidate) -> dict[str, Any]:
    return {
        "observed_at": cand.observed_at.isoformat(),
        "scene_id": cand.scene_id,
        "candidate_id": cand.id,
        "priority_score": score.priority_score,
        "severity": score.severity,
        "confidence": score.confidence,
        "alertable": score.alertable,
        "natural_cause_likely": score.natural_cause_likely,
    }


def _append_timeline(alert: Alert, entry: dict[str, Any]) -> None:
    rows = [e for e in alert.timeline if e.get("candidate_id") != entry["candidate_id"]]
    rows.append(entry)
    rows.sort(key=lambda e: e["observed_at"])
    alert.timeline = rows
    alert.n_observations = sum(1 for e in rows if e.get("alertable"))


def _apply_latest(
    alert: Alert,
    score: CandidateScore,
    cand: AnomalyCandidate,
    zone: Zone,
    evidence: dict[str, Any],
) -> None:
    """Refresh the alert's current fields from its newest alertable observation."""
    assert score.severity is not None
    alert.severity = score.severity
    alert.confidence = score.confidence
    alert.priority_score = score.priority_score
    alert.natural_cause_likely = score.natural_cause_likely
    alert.affected_area_km2 = cand.affected_area_km2
    alert.model_version = score.model_version
    alert.last_observed_at = cand.observed_at
    alert.latest_candidate_id = cand.id
    alert.latest_scene_id = cand.scene_id
    alert.summary = score.summary
    alert.contributions = score.contributions
    alert.indicators = score.indicators
    alert.context = score.context
    alert.evidence = evidence
    geom = to_shape(cand.spatial_geom) if cand.spatial_geom is not None else to_shape(zone.geom)
    alert.geom = from_shape(geom, srid=4326)
    if score.priority_score > alert.peak_priority_score:
        alert.peak_priority_score = score.priority_score
    if severity_rank(score.severity) > severity_rank(alert.peak_severity):
        alert.peak_severity = score.severity


def upsert_alert(
    session: Session,
    *,
    wb: WaterBody,
    zone: Zone,
    scene: Scene,
    cand: AnomalyCandidate,
    score: CandidateScore,
    evidence: dict[str, Any],
    settings: Settings,
) -> tuple[Alert | None, Action, bool]:
    """Returns (alert, action, escalated)."""
    entry = timeline_entry(score, cand)
    alertable = score.alertable and score.priority_score >= settings.alert_min_priority

    if not alertable or score.primary_indicator is None or score.severity is None:
        open_alert = find_open_alert_for_zone(
            session, zone.id, cand.observed_at, window_days=settings.alert_dedupe_days
        )
        if open_alert is None:
            return None, "skipped", False
        _append_timeline(open_alert, entry)
        return open_alert, "appended", False

    existing = find_open_alert(
        session,
        zone.id,
        score.primary_indicator,
        cand.observed_at,
        window_days=settings.alert_dedupe_days,
    )
    if existing is not None:
        before = severity_rank(existing.severity)
        _append_timeline(existing, entry)
        if cand.observed_at >= existing.last_observed_at:
            _apply_latest(existing, score, cand, zone, evidence)
        if cand.observed_at < existing.first_observed_at:
            existing.first_observed_at = cand.observed_at
        escalated = severity_rank(existing.severity) > before
        return existing, "updated", escalated

    alert_id = alert_id_for(wb, zone, cand.observed_at)
    if session.get(Alert, alert_id) is not None:
        # Same zone/day already produced an alert on another indicator: suffix it.
        alert_id = f"{alert_id}_{slug(score.primary_indicator)}"
    alert = Alert(
        id=alert_id,
        water_body_id=wb.id,
        zone_id=zone.id,
        primary_indicator=score.primary_indicator,
        status="open",
        peak_priority_score=score.priority_score,
        peak_severity=score.severity,
        first_observed_at=cand.observed_at,
        n_observations=1,
        timeline=[entry],
        status_changed_at=datetime.now(UTC),
        # the rest is set by _apply_latest
        severity=score.severity,
        confidence=score.confidence,
        priority_score=score.priority_score,
        model_version=score.model_version,
        last_observed_at=cand.observed_at,
        summary=score.summary,
        geom=from_shape(to_shape(zone.geom), srid=4326),
    )
    _apply_latest(alert, score, cand, zone, evidence)
    session.add(alert)
    session.flush()
    return alert, "created", True


def assemble_scene(
    session: Session,
    wb: WaterBody,
    scene: Scene,
    *,
    settings: Settings | None = None,
) -> AssembleResult:
    """Turn every scored candidate of one (water body, scene) into alert
    creations / updates. Idempotent: re-running appends nothing twice."""
    settings = settings or get_settings()
    result = AssembleResult(water_body_id=wb.id, scene_id=scene.id)
    rows = session.execute(
        select(CandidateScore, AnomalyCandidate, Zone)
        .join(AnomalyCandidate, AnomalyCandidate.id == CandidateScore.candidate_id)
        .join(Zone, Zone.id == CandidateScore.zone_id)
        .where(CandidateScore.water_body_id == wb.id, CandidateScore.scene_id == scene.id)
        .order_by(CandidateScore.priority_score.desc())
    ).all()
    if not rows:
        return result
    l6 = session.execute(
        select(IndicatorRun.chips).where(
            IndicatorRun.water_body_id == wb.id, IndicatorRun.scene_id == scene.id
        )
    ).scalar_one_or_none()
    mask_key = session.execute(
        select(WaterMaskRecord.chip_key).where(
            WaterMaskRecord.water_body_id == wb.id, WaterMaskRecord.scene_id == scene.id
        )
    ).scalar_one_or_none()
    for score, cand, zone in rows:
        evidence: dict[str, Any] = {}
        if score.primary_indicator:
            links = evidence_links(
                session,
                wb.id,
                score.primary_indicator,
                scene,
                current_chips=l6 or {},
                mask_chip_key=mask_key,
            )
            # alert id is only known after upsert for new alerts; fill the id-bound urls below
            evidence = links.to_dict("__alert_id__")
        alert, action, escalated = upsert_alert(
            session,
            wb=wb,
            zone=zone,
            scene=scene,
            cand=cand,
            score=score,
            evidence=evidence,
            settings=settings,
        )
        if alert is not None and evidence:
            alert.evidence = {
                k: (v.replace("__alert_id__", alert.id) if isinstance(v, str) else v)
                for k, v in evidence.items()
            }
        if action == "created":
            assert alert is not None
            result.created.append(alert.id)
            metrics.alerts_raised.labels(action="created", severity=alert.severity).inc()
        elif action == "updated":
            assert alert is not None
            result.updated.append(alert.id)
            metrics.alerts_raised.labels(action="updated", severity=alert.severity).inc()
            if escalated:
                result.escalated.append(alert.id)
        elif action == "appended":
            assert alert is not None
            result.appended.append(alert.id)
        else:
            result.skipped += 1
    session.flush()
    log.info(
        "alerts assembled",
        extra={
            "water_body_id": wb.id,
            "scene_id": scene.id,
            "alerts_created": result.created,
            "updated": len(result.updated),
            "escalated": result.escalated,
            "appended": len(result.appended),
            "skipped": result.skipped,
        },
    )
    return result


def set_status(
    session: Session, alert: Alert, status: str, *, by: str | None = None, note: str | None = None
) -> Alert:
    if status not in ("open", "investigating", "validated", "dismissed"):
        raise ValueError(f"invalid alert status {status!r}")
    alert.status = status
    alert.status_changed_at = datetime.now(UTC)
    alert.status_changed_by = by
    alert.status_note = note
    session.flush()
    return alert
