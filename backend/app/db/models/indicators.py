"""Zone-level indicator time series (S4, L6).

``indicator_observations`` is a TimescaleDB hypertable partitioned on
``observed_at`` (created in migration 0005; SQLAlchemy sees a plain table).
``indicator_runs`` is the per-(water body, scene) bookkeeping row that makes the
Celery task idempotent and keeps the list of rejected zone records.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IndicatorObservation(Base):
    """One row per (scene, zone, indicator). Rows below the quality threshold are
    never written, so a gap in the series means "could not observe", never zero."""

    __tablename__ = "indicator_observations"

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    zone_id: Mapped[str] = mapped_column(
        Text, ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True
    )
    indicator: Mapped[str] = mapped_column(Text, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        Text, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mean: Mapped[float | None] = mapped_column(Float)
    p90: Mapped[float | None] = mapped_column(Float)
    std: Mapped[float | None] = mapped_column(Float)
    n_pixels: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_pixel_pct: Mapped[float] = mapped_column(REAL, nullable=False)
    water_fraction_pct: Mapped[float] = mapped_column(REAL, nullable=False)
    clipped_pct: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndicatorRun(Base):
    __tablename__ = "indicator_runs"
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
    n_observations: Mapped[int | None] = mapped_column(Integer)
    # [{zone_id, indicator, reason, valid_pixel_pct, n_pixels}, ...]
    rejected: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # indicator -> {chip_key, clipped_pct, n_pixels}
    chips: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    boa_offset: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
