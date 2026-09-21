"""Bulk historical backfill planning (S5).

A baseline needs years of observations, so a newly registered body must run the
whole L3 -> L4/L5 -> L6 chain over its archive. The chain already exists as
``ingest_water_body`` (which enqueues masks, which enqueue indicators), so the
backfill is just a list of date chunks handed to that task. Chunks are bounded
so a failed month retries alone and the ingestion queue stays fair between
bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.core.config import Settings, get_settings

S2_L2A_ARCHIVE_START = date(2017, 1, 1)  # Earth Search / CDSE L2A coverage over India


@dataclass(frozen=True)
class BackfillPlan:
    water_body_id: str
    date_from: date
    date_to: date
    chunks: list[tuple[date, date]]

    @property
    def n_chunks(self) -> int:
        return len(self.chunks)


def plan_backfill(
    water_body_id: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    settings: Settings | None = None,
    today: date | None = None,
) -> BackfillPlan:
    """Split [date_from, date_to] into ``baseline_backfill_chunk_days``-long
    inclusive chunks. Defaults: ``baseline_history_years`` back from today."""
    settings = settings or get_settings()
    today = today or datetime.now(UTC).date()
    date_to = min(date_to or today, today)
    if date_from is None:
        date_from = date_to - timedelta(days=365 * settings.baseline_history_years)
    date_from = max(date_from, S2_L2A_ARCHIVE_START)
    chunks: list[tuple[date, date]] = []
    start = date_from
    step = max(settings.baseline_backfill_chunk_days, 1)
    while start <= date_to:
        end = min(date_to, start + timedelta(days=step - 1))
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return BackfillPlan(water_body_id, date_from, date_to, chunks)
