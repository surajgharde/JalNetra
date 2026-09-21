"""Priority scoring and explanation (S7, L9 + L10).

``candidate_scores`` is 1:1 with ``anomaly_candidates``: the fused priority
score, the confidence (kept separate on purpose), the final severity after the
S6 rainfall cap, the feature vector the model saw, the four signed
contributions and the plain-English summary. Rescoring with a newer model
overwrites the row; ``model_version`` says which model produced it.

``priority_models`` versions every model. The weighted model is code, not a
file, and is always available as the fallback; XGBoost models are stored in
MinIO and listed here with their training metrics.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CandidateScore(Base):
    __tablename__ = "candidate_scores"

    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("anomaly_candidates.id", ondelete="CASCADE"), primary_key=True
    )
    # Denormalised so the alert feed (S8) and the map never join back to L8.
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zone_id: Mapped[str] = mapped_column(
        Text, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scene_id: Mapped[str] = mapped_column(
        Text, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    priority_score: Mapped[float] = mapped_column(REAL, nullable=False)  # 0..100
    severity: Mapped[str | None] = mapped_column(Text)  # low | medium | high; NULL = nothing to see
    severity_capped_from: Mapped[str | None] = mapped_column(Text)  # rainfall gate downgrade
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)  # 0..1
    # {observation, baseline, agreement} - the three confidence inputs, for the UI
    confidence_parts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    primary_indicator: Mapped[str | None] = mapped_column(Text)
    alertable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    natural_cause_likely: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    model_version: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    model_base: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )  # score with all features at 0
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # exactly four: [{factor, key, value, raw}, ...], signed, sum within 10% of score - base
    contributions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # [{key, value, baseline_mean, baseline_std, z_score, deviation_pct}, ...] - API shape
    indicators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # {rainfall_72h_mm, rainfall_percentile, cloud_cover_pct, natural_cause_likely, gate_reason}
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PriorityModel(Base):
    __tablename__ = "priority_models"

    version: Mapped[str] = mapped_column(
        Text, primary_key=True
    )  # e.g. weighted-v1, xgb-20260921-01
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # weighted | xgboost
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    s3_key: Mapped[str | None] = mapped_column(Text)  # xgboost booster JSON in MinIO
    n_training: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    feature_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
