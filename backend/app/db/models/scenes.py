"""Sentinel-2 scenes and per-water-body ingestion records (S2, L3)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Scene(Base):
    """One row per Sentinel-2 pass over one MGRS tile (STAC item)."""

    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # STAC item id
    mgrs_tile: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    sensed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    platform: Mapped[str | None] = mapped_column(Text)
    cloud_pct: Mapped[float] = mapped_column(Float, nullable=False)
    stac_href: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # earth-search | cdse
    # Tile-level usability (cloud threshold). S3 refines it per water body from the SCL mask.
    usable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    epsg: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ingestions: Mapped[list["SceneIngestion"]] = relationship(back_populates="scene")


class SceneIngestion(Base):
    """Windowed band arrays of one scene over one water body, cached in object storage.
    Idempotency key for the ingest task: (water_body_id, scene_id)."""

    __tablename__ = "scene_ingestions"
    __table_args__ = (UniqueConstraint("water_body_id", "scene_id", name="water_body_scene"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scene_id: Mapped[str] = mapped_column(
        Text, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )  # pending|done|failed
    cache_key: Mapped[str | None] = mapped_column(Text)
    bytes_read: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    scene: Mapped[Scene] = relationship(back_populates="ingestions")
