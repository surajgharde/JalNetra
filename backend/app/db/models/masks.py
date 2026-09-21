"""Per-(water body, scene) preprocessing verdicts, water masks and raster chips (S3, L4+L5)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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


class WaterMaskRecord(Base):
    """One row per (water body, scene): the SCL validity verdict for this body's AOI
    and, when usable, the water mask summary. ``scenes.usable`` stays tile-level;
    this is the per-body refinement the plan's "mark usable = false" refers to."""

    __tablename__ = "water_masks"
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
    )  # pending|done|failed

    # L4
    aoi_pixels: Mapped[int | None] = mapped_column(Integer)
    valid_pixel_pct: Mapped[float | None] = mapped_column(Float)
    cloud_pixel_pct: Mapped[float | None] = mapped_column(Float)
    usable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # L5 (null when not usable)
    mndwi_threshold: Mapped[float | None] = mapped_column(Float)
    threshold_method: Mapped[str | None] = mapped_column(Text)  # otsu | fallback
    water_pixels: Mapped[int | None] = mapped_column(Integer)
    water_extent_km2: Mapped[float | None] = mapped_column(Float)
    water_fraction_pct: Mapped[float | None] = mapped_column(Float)
    n_components: Mapped[int | None] = mapped_column(Integer)
    chip_key: Mapped[str | None] = mapped_column(Text)

    error: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RasterChip(Base):
    """COG chips for map display. Rasters never go in the database -- only keys.
    ``zone_id`` is NULL for whole-body layers (S3 water mask); S4 adds zone chips."""

    __tablename__ = "raster_chips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zone_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("zones.id", ondelete="CASCADE"), index=True
    )
    scene_id: Mapped[str] = mapped_column(
        Text, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    layer: Mapped[str] = mapped_column(Text, nullable=False)  # watermask|truecolor|ndti|...
    s3_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    bytes: Mapped[int | None] = mapped_column(Integer)
    # [minx, miny, maxx, maxy] in EPSG:4326 for the map; native CRS kept for tooling.
    bounds: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    crs: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
