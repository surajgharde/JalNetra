"""Prometheus metrics (S12). One registry per process: the API exposes it at
``GET /metrics``; every Celery worker pool serves it on ``WORKER_METRICS_PORT``
(see ``app.workers.signals``). Prometheus scrapes them all.

Counters are incremented where the fact happens (ingestion, mask, alerts) so
they are correct even when a task is retried; ``task_duration_seconds`` comes
from the Celery prerun/postrun signals and is labelled by stage.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

registry = CollectorRegistry(auto_describe=True)

scenes_ingested = Counter(
    "jalnetra_scenes_ingested_total",
    "Scenes whose bands were windowed-read and cached",
    ["source"],
    registry=registry,
)
scenes_rejected_cloud = Counter(
    "jalnetra_scenes_rejected_cloud_total",
    "Scenes rejected for cloud: at STAC (tile-level) or by the L4 mask (body-level)",
    ["stage"],
    registry=registry,
)
zones_processed = Counter(
    "jalnetra_zones_processed_total",
    "Zone-scene records written by L6 (accepted) or refused (rejected)",
    ["outcome"],
    registry=registry,
)
alerts_raised = Counter(
    "jalnetra_alerts_raised_total",
    "Alerts created (new) or updated by a later observation",
    ["action", "severity"],
    registry=registry,
)
alerts_gated_rainfall = Counter(
    "jalnetra_alerts_gated_rainfall_total",
    "Candidates marked natural_cause_likely by the rainfall gate",
    registry=registry,
)
stac_request_failures = Counter(
    "jalnetra_stac_request_failures_total",
    "STAC searches that failed on a source (fallback may still have succeeded)",
    ["source"],
    registry=registry,
)
stac_fallbacks = Counter(
    "jalnetra_stac_fallbacks_total",
    "Searches served by a secondary source after the primary failed",
    ["served_by"],
    registry=registry,
)
task_duration = Histogram(
    "jalnetra_task_duration_seconds",
    "Celery task wall time by pipeline stage",
    ["stage", "status"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1800),
    registry=registry,
)
dispatches = Counter(
    "jalnetra_dispatches_total",
    "Alert deliveries by channel and outcome",
    ["channel", "status"],
    registry=registry,
)

# Gauges refreshed by the `refresh_ops_gauges` beat task from the database.
tier1_days_since_usable_scene = Gauge(
    "jalnetra_tier1_days_since_usable_scene",
    "Days since the last usable (masked) scene per Tier 1 water body",
    ["water_body_id"],
    registry=registry,
)
open_alerts = Gauge(
    "jalnetra_open_alerts",
    "Open + investigating alerts by severity",
    ["severity"],
    registry=registry,
)
baseline_building_zones = Gauge(
    "jalnetra_baseline_building_zones",
    "Zones whose seasonal baseline is still building (alerts suppressed)",
    registry=registry,
)
validation_precision = Gauge(
    "jalnetra_validation_precision",
    "matched / (matched + not_matched) over all conclusive field validations",
    registry=registry,
)

# Task name -> stage label, so dashboards read "mask" rather than a dotted path.
STAGE_OF_TASK: dict[str, str] = {
    "app.workers.tasks.ingest_water_body": "ingestion",
    "app.workers.tasks.poll_tier_scenes": "poll",
    "app.workers.tasks.poll_tier1_scenes": "poll",
    "app.workers.tasks.compute_water_mask": "mask",
    "app.workers.tasks.compute_indicators": "indicators",
    "app.workers.tasks.detect_anomalies": "anomalies",
    "app.workers.tasks.score_candidates": "scoring",
    "app.workers.tasks.assemble_alerts": "alerts",
    "app.workers.tasks.generate_brief": "brief",
    "app.workers.tasks.dispatch_alert": "dispatch",
    "app.workers.tasks.build_baselines": "baseline",
    "app.workers.tasks.sync_rainfall": "rainfall",
    "app.workers.tasks.backfill_history": "backfill",
}


def stage_of(task_name: str) -> str:
    return STAGE_OF_TASK.get(task_name, task_name.rsplit(".", 1)[-1])


def render() -> bytes:
    return bytes(generate_latest(registry))
