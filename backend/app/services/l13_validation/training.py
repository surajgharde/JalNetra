"""Retraining on validated outcomes (S11).

* ``load_validated_examples`` - every conclusive validation whose alert still
  has its scored feature vector becomes a labelled example: target
  ``retrain_target_matched`` (90) for a corroborated alert,
  ``retrain_target_not_matched`` (10) for a field-confirmed false positive.
* ``retrain`` - refuses cleanly below ``priority_train_min_validations`` (a
  logged reason, not an exception), otherwise fits an XGBoost priority model
  on a training split, measures **precision at the alert threshold** on the
  held-out split for both the candidate and the currently active model, and
  promotes the candidate only if its precision is strictly better. A worse
  model is registered (for the record) but never activated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import CandidateScore, Validation
from app.services.l09_fusion.features import FEATURE_NAMES, FeatureVector
from app.services.l09_fusion.models import (
    LabelledExample,
    PriorityModel,
    XGBoostModel,
    activate_model,
    active_model,
    train_xgboost,
)

log = logging.getLogger(__name__)


@dataclass
class RetrainResult:
    trained: bool
    promoted: bool
    reason: str
    version: str | None = None
    n_examples: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trained": self.trained,
            "promoted": self.promoted,
            "reason": self.reason,
            "version": self.version,
            "n_examples": self.n_examples,
            "metrics": self.metrics,
        }


def feature_vector_from_score(score: CandidateScore) -> FeatureVector | None:
    feats = score.features or {}
    norm = feats.get("norm") or {}
    raw = feats.get("raw") or {}
    if any(k not in norm for k in FEATURE_NAMES):
        return None
    return FeatureVector(
        raw={k: raw.get(k) for k in FEATURE_NAMES},
        norm={k: float(norm[k]) for k in FEATURE_NAMES},
    )


def load_validated_examples(
    session: Session, *, settings: Settings | None = None
) -> list[LabelledExample]:
    settings = settings or get_settings()
    rows = session.execute(
        select(Validation, CandidateScore)
        .join(CandidateScore, CandidateScore.candidate_id == Validation.candidate_id)
        .where(Validation.verdict.in_(("matched", "not_matched")))
        .order_by(Validation.created_at)
    ).all()
    out: list[LabelledExample] = []
    for v, score in rows:
        fv = feature_vector_from_score(score)
        if fv is None:
            continue
        target = (
            settings.retrain_target_matched
            if v.verdict == "matched"
            else settings.retrain_target_not_matched
        )
        out.append(LabelledExample(features=fv, target=target))
    return out


def precision_at_threshold(
    model: PriorityModel,
    examples: list[LabelledExample],
    *,
    threshold: float,
    matched_target: float,
) -> tuple[float | None, int]:
    """Share of examples the model would alert on (score >= threshold) that
    were field-corroborated. (precision, n_predicted_alerts)."""
    predicted = [e for e in examples if model.attribute(e.features).score >= threshold]
    if not predicted:
        return None, 0
    hits = sum(1 for e in predicted if e.target >= matched_target)
    return hits / len(predicted), len(predicted)


def retrain(
    session: Session, store: ObjectStore, *, settings: Settings | None = None
) -> RetrainResult:
    settings = settings or get_settings()
    examples = load_validated_examples(session, settings=settings)
    n = len(examples)
    if n < settings.priority_train_min_validations:
        reason = (
            f"{n} conclusive validations with scored features; retraining needs "
            f"{settings.priority_train_min_validations}. Keeping the active model."
        )
        log.info("retrain skipped", extra={"n": n, "reason": reason})
        return RetrainResult(False, False, reason, n_examples=n)

    rng = np.random.default_rng(settings.multivariate_seed)
    idx = rng.permutation(n)
    n_hold = max(int(0.25 * n), 5)
    hold = [examples[i] for i in idx[:n_hold]]
    train = [examples[i] for i in idx[n_hold:]]
    if len(train) < settings.priority_train_min_validations // 2:
        reason = f"only {len(train)} training examples after the hold-out split"
        return RetrainResult(False, False, reason, n_examples=n)

    incumbent = active_model(session, store, settings=settings)
    try:
        row = train_xgboost(
            session, store, train, settings=settings, activate=False, min_examples=len(train)
        )
    except ImportError as exc:
        reason = f"xgboost/shap not installed ({exc}); install the `ml` extra"
        log.warning("retrain skipped", extra={"reason": reason})
        return RetrainResult(False, False, reason, n_examples=n)

    candidate = XGBoostModel.from_json(store.get_bytes(row.s3_key or ""), row.version)
    p_new, k_new = precision_at_threshold(
        candidate,
        hold,
        threshold=settings.retrain_alert_threshold,
        matched_target=settings.retrain_target_matched,
    )
    p_old, k_old = precision_at_threshold(
        incumbent,
        hold,
        threshold=settings.retrain_alert_threshold,
        matched_target=settings.retrain_target_matched,
    )
    metrics = {
        **row.metrics,
        "holdout_n": n_hold,
        "candidate_precision": None if p_new is None else round(p_new, 3),
        "candidate_predicted_alerts": k_new,
        "incumbent_version": incumbent.version,
        "incumbent_precision": None if p_old is None else round(p_old, 3),
        "incumbent_predicted_alerts": k_old,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    row.metrics = metrics
    session.flush()

    better = p_new is not None and (p_old is None or p_new > p_old)
    new_s = "-" if p_new is None else f"{p_new:.3f}"
    old_s = "-" if p_old is None else f"{p_old:.3f}"
    if better:
        activate_model(session, row.version)
        reason = f"promoted: precision {new_s} > {old_s} ({incumbent.version})"
    else:
        reason = f"not promoted: precision {new_s} is not better than {old_s} ({incumbent.version})"
    log.info("retrain done", extra={"version": row.version, "promoted": better, "reason": reason})
    return RetrainResult(True, better, reason, version=row.version, n_examples=n, metrics=metrics)
