"""Water body inventory: registered water bodies and their monitoring zones (S1)."""

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class WaterBody(Base):
    __tablename__ = "water_bodies"
    __table_args__ = (CheckConstraint("tier IN (1, 2, 3)", name="tier_range"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. wb_khadakwasla
    name: Mapped[str] = mapped_column(Text, nullable=False)
    district: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="reservoir"
    )  # reservoir | lake | river_stretch
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, index=True)
    # MULTIPOLYGON so river stretches and braided reservoirs fit without special-casing.
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True), nullable=False
    )
    area_km2: Mapped[float] = mapped_column(Float, nullable=False)
    mgrs_tiles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    zones: Mapped[list["Zone"]] = relationship(
        back_populates="water_body", cascade="all, delete-orphan", order_by="Zone.seq"
    )


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. wb_khadakwasla_z3
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # "Eastern zone"
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True), nullable=False
    )
    area_km2: Mapped[float] = mapped_column(Float, nullable=False)

    water_body: Mapped[WaterBody] = relationship(back_populates="zones")
