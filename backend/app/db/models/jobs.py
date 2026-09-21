"""Pipeline jobs (S9) and ground/lab validations (S9 storage, S11 verdicts).

A ``jobs`` row is what ``POST /api/v1/jobs/ingest`` returns: it names a water
body and a date window and remembers the Celery task that kicked the chain
off. Progress is *derived* on read from the per-stage run tables (scene
ingestions, masks, indicator runs, anomaly runs, scores) rather than pushed by
the tasks, so it stays truthful across retries and restarts.

``validations`` stores field / laboratory results submitted against an alert.
S9 stores them; S11 computes ``verdict`` and feeds them back into the model.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    REAL,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

JOB_STATUSES = ("queued", "running", "done", "failed")
VALIDATION_VERDICTS = ("matched", "not_matched", "inconclusive")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # job_<uuid4 hex>
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # ingest | backfill
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued", index=True)
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # last derived snapshot, for the list endpoint
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Validation(Base):
    __tablename__ = "validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(
        Text, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sampled_on: Mapped[date] = mapped_column(Date, nullable=False)
    # {turbidity_ntu, chlorophyll_ug_l, tss_mg_l, do_mg_l, ph, ...} - all optional
    lab_results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    observed_condition: Mapped[str | None] = mapped_column(Text)  # free text, what the officer saw
    notes: Mapped[str | None] = mapped_column(Text)
    photo_key: Mapped[str | None] = mapped_column(Text)  # MinIO key (S11 upload)
    submitted_by: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(Text)  # matched | not_matched | inconclusive (S11)
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    # Snapshot of the alert at submission, so precision is judged against what the
    # officer actually saw even if the alert is later rescored or reassigned.
    alert_severity: Mapped[str | None] = mapped_column(Text)
    alert_indicator: Mapped[str | None] = mapped_column(Text)
    alert_priority_score: Mapped[float | None] = mapped_column(REAL)
    alert_observed_on: Mapped[date | None] = mapped_column(Date)
    candidate_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("anomaly_candidates.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BaselineSample(Base):
    """Extra baseline observations fed back from field validation (S11).

    A ``not_matched`` verdict says the water was normal on that date; the zone
    mean the satellite saw then is therefore a *confirmed* normal value, and it
    is added to the seasonal history so the band learns from it. L7's
    ``load_history`` unions these rows with the hypertable.
    """

    __tablename__ = "baseline_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_id: Mapped[str] = mapped_column(
        Text, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    indicator: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(REAL, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="validation")
    validation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("validations.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
