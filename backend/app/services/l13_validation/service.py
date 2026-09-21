"""L13 validation loop (S11): submit -> verdict -> alert status -> feedback.

On submit:
1. the verdict is computed against the alert's primary indicator;
2. the alert moves to ``validated`` / ``dismissed`` / ``investigating``;
3. a ``not_matched`` verdict feeds the satellite value the alert was raised on
   back into the seasonal history as a confirmed-normal sample (L7 reads it);
4. a snapshot of the alert (severity, indicator, priority, candidate) is kept on
   the validation row so precision is judged against what was actually shown.

``precision_summary`` is the number that proves the system works: the share of
conclusive validations that matched, overall and broken down by indicator and
by severity band.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import (
    Alert,
    AnomalyCandidate,
    BaselineSample,
    Validation,
)
from app.services.l06_indicators.registry import QUALITY_INDICATOR_KEYS
from app.services.l11_alerts.assembler import set_status
from app.services.l13_validation.verdict import VerdictResult, compute_verdict, status_for_verdict

log = logging.getLogger(__name__)

PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@dataclass(frozen=True)
class SubmitResult:
    validation: Validation
    verdict: VerdictResult
    alert_status: str
    baseline_samples_added: int


def submit_validation(
    session: Session,
    *,
    alert: Alert,
    sampled_on: date,
    lab_results: dict[str, Any],
    observed_condition: str | None,
    notes: str | None,
    submitted_by: str | None,
    settings: Settings | None = None,
) -> SubmitResult:
    settings = settings or get_settings()
    observed_on = alert.last_observed_at.date()
    verdict = compute_verdict(
        primary_indicator=alert.primary_indicator,
        observed_on=observed_on,
        sampled_on=sampled_on,
        lab_results=lab_results,
        thresholds=settings.validation_thresholds,
        max_lag_days=settings.validation_max_lag_days,
    )
    row = Validation(
        alert_id=alert.id,
        sampled_on=sampled_on,
        lab_results=lab_results,
        observed_condition=observed_condition,
        notes=notes,
        submitted_by=submitted_by,
        verdict=verdict.verdict,
        verdict_reason=verdict.reason,
        alert_severity=alert.severity,
        alert_indicator=alert.primary_indicator,
        alert_priority_score=alert.priority_score,
        alert_observed_on=observed_on,
        candidate_id=alert.latest_candidate_id,
    )
    session.add(row)
    session.flush()

    new_status = status_for_verdict(verdict.verdict)
    set_status(
        session,
        alert,
        new_status,
        by=submitted_by or "field validation",
        note=f"validation #{row.id}: {verdict.verdict} - {verdict.reason}",
    )

    added = 0
    if verdict.verdict == "not_matched":
        added = feed_back_true_negative(session, alert, row)
    log.info(
        "validation submitted",
        extra={
            "alert_id": alert.id,
            "validation_id": row.id,
            "verdict": verdict.verdict,
            "alert_status": new_status,
            "baseline_samples": added,
        },
    )
    return SubmitResult(row, verdict, new_status, added)


def feed_back_true_negative(session: Session, alert: Alert, validation: Validation) -> int:
    """The satellite values the alert was raised on are confirmed normal: add
    them to the zone's seasonal history for every quality indicator observed."""
    cand = (
        session.get(AnomalyCandidate, alert.latest_candidate_id)
        if alert.latest_candidate_id
        else None
    )
    if cand is None:
        return 0
    n = 0
    for key in QUALITY_INDICATOR_KEYS:
        value = (cand.indicator_values or {}).get(key, {}).get("mean")
        if value is None:
            continue
        session.add(
            BaselineSample(
                zone_id=alert.zone_id,
                indicator=key,
                observed_at=cand.observed_at,
                value=float(value),
                source="validation",
                validation_id=validation.id,
            )
        )
        n += 1
    session.flush()
    return n


# --- photos ----------------------------------------------------------------------


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "photo"


def store_photo(
    session: Session,
    store: ObjectStore,
    validation: Validation,
    *,
    data: bytes,
    content_type: str,
    filename: str,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    if content_type not in PHOTO_TYPES:
        raise ValueError(f"unsupported photo type {content_type!r}; use JPEG, PNG or WebP")
    if len(data) > settings.validation_photo_max_bytes:
        raise ValueError(
            f"photo larger than {settings.validation_photo_max_bytes // (1024 * 1024)} MB"
        )
    key = f"{settings.validation_photo_prefix}/{validation.id}/{safe_filename(filename)}"
    store.put_bytes(key, data, content_type=content_type)
    validation.photo_key = key
    session.flush()
    return key


# --- summary ----------------------------------------------------------------------


def _bucket(rows: list[tuple[str | None, str | None]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for group, verdict in rows:
        g = out.setdefault(
            group or "unknown",
            {"matched": 0, "not_matched": 0, "inconclusive": 0, "precision": None},
        )
        if verdict in ("matched", "not_matched", "inconclusive"):
            g[verdict] += 1
    for g in out.values():
        concl = g["matched"] + g["not_matched"]
        g["precision"] = round(g["matched"] / concl, 3) if concl else None
        g["n"] = concl + g["inconclusive"]
    return out


def precision_summary(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(Validation.alert_indicator, Validation.alert_severity, Validation.verdict)
    ).all()
    overall = _bucket([("all", r[2]) for r in rows]).get(
        "all", {"matched": 0, "not_matched": 0, "inconclusive": 0, "precision": None, "n": 0}
    )
    last = session.execute(select(func.max(Validation.created_at))).scalar_one_or_none()
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "last_validation_at": last.isoformat() if last else None,
        "overall": overall,
        "by_indicator": _bucket([(r[0], r[2]) for r in rows]),
        "by_severity": _bucket([(r[1], r[2]) for r in rows]),
        "note": (
            "precision = matched / (matched + not_matched) over field-validated alerts; "
            "inconclusive samples are counted but excluded from the ratio"
        ),
    }
