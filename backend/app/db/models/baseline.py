"""Seasonal baselines and the rainfall covariate (S5, L7).

``baselines`` holds one row per (zone, indicator, day-of-year): the robust
statistics of every historical observation that falls inside a window centred
on that DOY. ``rainfall`` holds one row per (water body, day) from Open-Meteo.
Neither table stores rasters; the composite chips referenced in the plan's
object-store layout are an L9/L10 concern.
"""

from datetime import date, datetime

from sqlalchemy import (
    REAL,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Baseline(Base):
    """Robust seasonal band for one (zone, indicator) at one day-of-year.

    ``mean``/``std`` keep the plan's column names but are the *median* and the
    MAD-derived sigma (1.4826 * MAD), so one historical spike does not inflate
    the band. ``status`` is ``usable`` once ``n_samples`` clears the minimum and
    the history span clears the tier's requirement; otherwise ``building`` and
    L8 must suppress alerts for that window.
    """

    __tablename__ = "baselines"

    zone_id: Mapped[str] = mapped_column(
        Text, ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True
    )
    indicator: Mapped[str] = mapped_column(Text, primary_key=True)
    doy_window: Mapped[int] = mapped_column(SmallInteger, primary_key=True)  # centre DOY, 1..366
    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    mean: Mapped[float | None] = mapped_column(Float)  # median of the window
    std: Mapped[float | None] = mapped_column(Float)  # 1.4826 * MAD
    p10: Mapped[float | None] = mapped_column(Float)
    p90: Mapped[float | None] = mapped_column(Float)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    n_years: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # distinct years in window
    status: Mapped[str] = mapped_column(Text, nullable=False)  # usable | building
    history_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Rainfall(Base):
    """Daily precipitation at the water body centroid. ``mm_72h`` is the
    trailing three-day sum ending on ``date`` (inclusive)."""

    __tablename__ = "rainfall"

    water_body_id: Mapped[str] = mapped_column(
        Text, ForeignKey("water_bodies.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    mm_24h: Mapped[float] = mapped_column(REAL, nullable=False)
    mm_72h: Mapped[float | None] = mapped_column(REAL)
    source: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # open-meteo-archive | open-meteo-forecast
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
