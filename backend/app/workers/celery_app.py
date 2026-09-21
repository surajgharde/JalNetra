"""Celery application. Queues: default, ingestion, processing, reporting (briefs + delivery)."""

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
    task_routes={
        "app.workers.tasks.ingest_water_body": {"queue": "ingestion"},
        "app.workers.tasks.poll_tier1_scenes": {"queue": "ingestion"},
        "app.workers.tasks.compute_water_mask": {"queue": "processing"},
        "app.workers.tasks.process_water_body": {"queue": "processing"},
        "app.workers.tasks.compute_indicators": {"queue": "processing"},
        "app.workers.tasks.process_indicators": {"queue": "processing"},
        "app.workers.tasks.detect_anomalies": {"queue": "processing"},
        "app.workers.tasks.process_anomalies": {"queue": "processing"},
        "app.workers.tasks.score_candidates": {"queue": "processing"},
        "app.workers.tasks.process_scores": {"queue": "processing"},
        "app.workers.tasks.train_priority_model": {"queue": "processing"},
        "app.workers.tasks.assemble_alerts": {"queue": "processing"},
        "app.workers.tasks.process_alerts": {"queue": "processing"},
        "app.workers.tasks.generate_brief": {"queue": "reporting"},
        "app.workers.tasks.dispatch_alert": {"queue": "reporting"},
        "app.workers.tasks.build_baselines": {"queue": "processing"},
        "app.workers.tasks.rebuild_all_baselines": {"queue": "processing"},
        "app.workers.tasks.sync_rainfall": {"queue": "ingestion"},
        "app.workers.tasks.sync_rainfall_all": {"queue": "ingestion"},
        "app.workers.tasks.backfill_history": {"queue": "ingestion"},
    },
    beat_schedule={
        "heartbeat-every-5-min": {
            "task": "app.workers.tasks.heartbeat",
            "schedule": crontab(minute="*/5"),
        },
        # Sentinel-2 revisits every 2-5 days and L2A lands within ~24 h; 6 h is plenty.
        "poll-tier1-scenes-every-6h": {
            "task": "app.workers.tasks.poll_tier1_scenes",
            "schedule": crontab(minute="15", hour="*/6"),
        },
        # Open-Meteo's archive lands with a ~5 day lag; one pull a day at 02:30 UTC
        # (08:00 IST) refreshes the lookback window for every body.
        "sync-rainfall-daily": {
            "task": "app.workers.tasks.sync_rainfall_all",
            "schedule": crontab(minute="30", hour="2"),
        },
        # Baselines only move as history accrues, so a nightly rebuild is enough.
        "rebuild-baselines-nightly": {
            "task": "app.workers.tasks.rebuild_all_baselines",
            "schedule": crontab(minute="0", hour="3"),
        },
    },
)

# Registers logging + job-id signal handlers.
from app.workers import signals as _signals  # noqa: E402, F401
