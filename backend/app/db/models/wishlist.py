"""Saved water bodies and recently inspected ones (S14).

Both tables are pure UI state layered on top of the registry: they reference
``water_bodies`` and hold nothing the pipeline reads, so seeding, ingestion and
scoring are untouched by anything here. Deleting a water body takes its wishlist
entry and view record with it.

There is no owner column. The API authenticates with one shared key per
deployment (``Settings.api_keys``), so there is no user to attribute a row to;
the wishlist is the deployment's, not a person's. An ``owner`` column can be
added later without disturbing either table.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.registry import WaterBody


class WishlistItem(Base):
    """A water body someone pinned, under a label that means something to them."""

    __tablename__ = "wishlist_items"
    # One entry per water body: saving the same lake twice is a rename, not a second row.
    __table_args__ = (UniqueConstraint("water_body_id", name="uq_wishlist_items_water_body_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    custom_name: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    water_body: Mapped[WaterBody] = relationship(lazy="joined")


class RecentWaterBody(Base):
    """Last time a water body was inspected. One row per water body, overwritten."""

    __tablename__ = "recent_water_bodies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    last_viewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    water_body: Mapped[WaterBody] = relationship(lazy="joined")
