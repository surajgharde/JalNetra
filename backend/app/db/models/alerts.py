"""Alerts, recipients and dispatch log (S8, L11 + L12).

An ``alerts`` row is the operational object an officer acts on. It is created
from an alertable ``candidate_scores`` row and *updated* - not duplicated -
by later scenes of the same zone and indicator while it is open (14-day
window), with every observation appended to ``timeline``. Its ``status`` is
the field workflow: open -> investigating -> validated | dismissed.

``recipients`` are the webhook / e-mail targets with a severity threshold and
an optional jurisdiction (district names or a polygon; matched point-in-polygon
on the zone centroid). ``dispatches`` logs every attempted delivery.
"""

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
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

ALERT_STATUSES = ("open", "investigating", "validated", "dismissed")
OPEN_STATUSES = ("open", "investigating")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # alr_2026_0917_khadakwasla_z3
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zone_id: Mapped[str] = mapped_column(
        Text, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    primary_indicator: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open", index=True)

    # Current verdict (latest observation) and the peak while open.
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # low | medium | high
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    priority_score: Mapped[float] = mapped_column(REAL, nullable=False, index=True)
    peak_priority_score: Mapped[float] = mapped_column(REAL, nullable=False)
    peak_severity: Mapped[str] = mapped_column(Text, nullable=False)
    natural_cause_likely: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    affected_area_km2: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)

    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    latest_candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("anomaly_candidates.id", ondelete="SET NULL"), nullable=True
    )
    latest_scene_id: Mapped[str] = mapped_column(
        Text, ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True
    )
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Frozen-contract pieces, copied from candidate_scores at the latest observation.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    contributions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    indicators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # {current_scene_id, reference_scene_id, chip keys, tile url templates}
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # [{observed_at, scene_id, candidate_id, priority_score, severity, confidence, alertable}]
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # Plume polygon of the latest observation, else the zone polygon.
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True), nullable=False
    )

    brief_key: Mapped[str | None] = mapped_column(Text)  # briefs/{alert_id}.pdf
    brief_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brief_for_observation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_severity: Mapped[str | None] = mapped_column(Text)  # highest severity dispatched
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status_changed_by: Mapped[str | None] = mapped_column(Text)
    status_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Recipient(Base):
    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # "MPCB Pune regional office"
    channel: Mapped[str] = mapped_column(Text, nullable=False)  # webhook | email | telegram
    target: Mapped[str] = mapped_column(Text, nullable=False)  # URL, e-mail address, or Telegram chat id
    secret: Mapped[str | None] = mapped_column(Text)  # webhook HMAC key
    min_severity: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    # Jurisdiction: any of these matches; none set = statewide.
    districts: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    jurisdiction_geom: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Dispatch(Base):
    __tablename__ = "dispatches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(
        Text, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # severity at dispatch
    reason: Mapped[str] = mapped_column(Text, nullable=False)  # new | escalated | manual
    status: Mapped[str] = mapped_column(Text, nullable=False)  # sent | failed | skipped
    detail: Mapped[str | None] = mapped_column(Text)  # HTTP status / SMTP reply / error
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
