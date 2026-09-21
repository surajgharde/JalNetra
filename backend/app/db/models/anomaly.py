"""Anomaly candidates (S6, L8).

One ``anomaly_candidates`` row per (zone, scene) whether or not anything was
flagged: L9 fuses over the full picture, and a row with zero votes is the
honest record that the zone was looked at and found ordinary. ``anomaly_runs``
is the per-(water body, scene) bookkeeping row that makes the task idempotent.

Product boundary: a candidate is an *observable deviation*. Nothing here says
"pollution", and ``natural_cause_likely`` exists precisely so a rain-explained
deviation is shown as downgraded rather than hidden.
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
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnomalyCandidate(Base):
    __tablename__ = "anomaly_candidates"
    __table_args__ = (UniqueConstraint("zone_id", "scene_id", name="zone_scene"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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

    # --- what was observed ---
    # indicator -> {mean, p90, valid_pixel_pct, water_fraction_pct}
    indicator_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # --- detector 1: temporal (robust z against the DOY baseline) ---
    # indicator -> {z, flagged, baseline_status, baseline_median, baseline_sigma, n_samples, reason}
    temporal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    temporal_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_abs_z: Mapped[float | None] = mapped_column(Float)
    anomalous_indicators: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # --- detector 2: spatial (DBSCAN clusters of hot pixels) ---
    # [{indicator, n_pixels, area_km2, mean_z, max_z, centroid: [lon, lat]}, ...]
    spatial: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    spatial_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    spatial_geom: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True), nullable=True
    )
    affected_area_km2: Mapped[float | None] = mapped_column(Float)

    # --- detector 3: multivariate (IsolationForest on the zone's own history) ---
    multivariate_score: Mapped[float | None] = mapped_column(Float)  # higher = more unusual
    multivariate_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # {n_history, features: {...}, fitted, reason}
    multivariate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # --- verdict of this layer (L9 refines confidence + priority) ---
    votes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    severity: Mapped[str | None] = mapped_column(Text)  # low | medium | high; NULL = no deviation
    natural_cause_likely: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # {applied, rainfall_72h, p90_doy, exceeded, anomalous_indicators, capped_from, reason}
    rainfall_gate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    rainfall_72h: Mapped[float | None] = mapped_column(REAL)
    baseline_status: Mapped[str] = mapped_column(Text, nullable=False)  # usable | building
    alertable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suppressed_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AnomalyRun(Base):
    __tablename__ = "anomaly_runs"
    __table_args__ = (UniqueConstraint("water_body_id", "scene_id", name="water_body_scene"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scene_id: Mapped[str] = mapped_column(
        Text, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sensed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )  # pending|done|skipped|failed
    n_zones: Mapped[int | None] = mapped_column(Integer)
    n_candidates: Mapped[int | None] = mapped_column(Integer)
    n_flagged: Mapped[int | None] = mapped_column(Integer)
    n_alertable: Mapped[int | None] = mapped_column(Integer)
    n_gated: Mapped[int | None] = mapped_column(Integer)
    spatial_ran: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
