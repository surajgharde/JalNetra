"""Celery application. Queues are split by cost in S12; S0 uses one default queue."""

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "jalnetra",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    beat_schedule={
        # Proves beat is alive; S2 replaces this with the Tier 1 scene poll.
        "heartbeat-every-5-min": {
            "task": "app.workers.tasks.heartbeat",
            "schedule": crontab(minute="*/5"),
        },
    },
)

# Registers logging + job-id signal handlers.
from app.workers import signals as _signals  # noqa: E402, F401
