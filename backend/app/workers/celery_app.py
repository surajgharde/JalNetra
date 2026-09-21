"""Celery application (S12 layout).

Queues split by cost so a slow job never blocks a cheap one; run one worker
pool per queue (docker-compose: worker-ingestion / -processing / -scoring /
-reporting):

* ingestion  - network-bound STAC searches, windowed band reads, rainfall
* processing - CPU-bound masks, indicators, detectors, baselines
* scoring    - fast: priority scoring, alert assembly, model retraining
* reporting  - PDF briefs and webhook / e-mail delivery
"""

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
        "app.workers.tasks.score_candidates": {"queue": "scoring"},
        "app.workers.tasks.process_scores": {"queue": "scoring"},
        "app.workers.tasks.train_priority_model": {"queue": "scoring"},
        "app.workers.tasks.assemble_alerts": {"queue": "scoring"},
        "app.workers.tasks.process_alerts": {"queue": "scoring"},
        "app.workers.tasks.refresh_ops_gauges": {"queue": "scoring"},
        "app.workers.tasks.poll_tier_scenes": {"queue": "ingestion"},
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
        # Sentinel-2 revisits every 2-5 days and L2A lands within ~24 h; Tier 1
        # every 6 h, Tier 2 daily, Tier 3 weekly (plan S12).
        "poll-tier1-scenes-every-6h": {
            "task": "app.workers.tasks.poll_tier_scenes",
            "schedule": crontab(minute="15", hour="*/6"),
            "args": (1,),
        },
        "poll-tier2-scenes-daily": {
            "task": "app.workers.tasks.poll_tier_scenes",
            "schedule": crontab(minute="45", hour="1"),
            "args": (2,),
        },
        "poll-tier3-scenes-weekly": {
            "task": "app.workers.tasks.poll_tier_scenes",
            "schedule": crontab(minute="45", hour="2", day_of_week="monday"),
            "args": (3,),
        },
        # Rainfall daily at 02:00 IST (20:30 UTC the previous day).
        "sync-rainfall-daily": {
            "task": "app.workers.tasks.sync_rainfall_all",
            "schedule": crontab(minute="30", hour="20"),
        },
        # Ops gauges for the Grafana alerting rules (stale Tier 1 bodies, open alerts).
        "refresh-ops-gauges": {
            "task": "app.workers.tasks.refresh_ops_gauges",
            "schedule": crontab(minute=f"*/{settings.ops_gauge_refresh_minutes}"),
        },
        # Weekly retraining attempt; exits with a logged reason until >= 50 validations exist.
        "retrain-priority-model-weekly": {
            "task": "app.workers.tasks.train_priority_model",
            "schedule": crontab(minute="30", hour="3", day_of_week="sunday"),
        },
        # Baselines only move as history accrues: monthly rebuild (plan S12).
        "rebuild-baselines-monthly": {
            "task": "app.workers.tasks.rebuild_all_baselines",
            "schedule": crontab(minute="0", hour="3", day_of_month="1"),
        },
    },
)

# Registers logging + job-id signal handlers.
from app.workers import signals as _signals  # noqa: E402, F401
