import logging
from datetime import date
from typing import Any

from celery import Task
from rasterio.errors import RasterioIOError

from app.core.config import get_settings
from app.core.storage import get_store
from app.db.sync_session import sync_session
from app.services.l03_ingestion.service import ingest_water_body as _ingest_water_body
from app.services.l03_ingestion.service import pending_scenes_for_tier
from app.services.l03_ingestion.stac import SourceError
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

TRANSIENT = (SourceError, RasterioIOError, OSError, ConnectionError, TimeoutError)


@celery_app.task(name="app.workers.tasks.ping")
def ping() -> str:
    log.info("ping")
    return "pong"


@celery_app.task(name="app.workers.tasks.heartbeat")
def heartbeat() -> None:
    log.info("beat heartbeat")


@celery_app.task(
    bind=True,
    name="app.workers.tasks.ingest_water_body",
    queue="ingestion",
    autoretry_for=TRANSIENT,
    retry_backoff=True,  # 1 s, 2 s, 4 s, ... exponential
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
    acks_late=True,
)
def ingest_water_body(
    self: Task, water_body_id: str, day: str, date_to: str | None = None
) -> dict[str, Any]:
    """Search and cache every usable scene for a water body on `day` (ISO date).
    Idempotent: scenes already cached are reported as skipped, not re-read."""
    log.info(
        "ingest start",
        extra={"water_body_id": water_body_id, "day": day, "attempt": self.request.retries},
    )
    with sync_session() as session:
        result = _ingest_water_body(
            session,
            get_store(),
            water_body_id,
            date.fromisoformat(day),
            date_to=date.fromisoformat(date_to) if date_to else None,
        )
        session.commit()
    payload = {
        "water_body_id": result.water_body_id,
        "scenes_found": result.scenes_found,
        "ingested": result.ingested,
        "skipped": result.skipped,
        "unusable": result.unusable,
        "failed": result.failed,
    }
    log.info("ingest done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.poll_tier1_scenes", queue="ingestion")
def poll_tier1_scenes() -> dict[str, Any]:
    """Beat job (every 6 h): find new usable Tier 1 scenes and enqueue their ingestion."""
    settings = get_settings()
    with sync_session() as session:
        pairs = pending_scenes_for_tier(
            session, tier=1, lookback_days=settings.ingest_lookback_days
        )
        session.commit()
    for wb_id, day in pairs:
        ingest_water_body.delay(wb_id, day.isoformat())
    log.info("tier1 poll", extra={"enqueued": len(pairs)})
    return {"enqueued": [[wb, d.isoformat()] for wb, d in pairs]}
