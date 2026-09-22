import logging
from datetime import date
from typing import Any

from celery import Task
from rasterio.errors import RasterioIOError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.storage import get_store
from app.db.sync_session import sync_session
from app.services.l03_ingestion.service import ingest_water_body as _ingest_water_body
from app.services.l03_ingestion.service import pending_scenes_for_tier
from app.services.l03_ingestion.stac import SourceError
from app.services.l05_water_detection.service import IngestionMissingError, scenes_needing_mask
from app.services.l05_water_detection.service import compute_water_mask as _compute_water_mask
from app.services.l05_water_detection.service import process_water_body as _process_water_body
from app.services.l06_indicators.service import MaskMissingError, scenes_needing_indicators
from app.services.l06_indicators.service import compute_indicators as _compute_indicators
from app.services.l06_indicators.service import process_water_body as _process_indicators
from app.services.l07_baseline.backfill import plan_backfill
from app.services.l07_baseline.rainfall import (
    RainfallSourceError,
    backfill_rainfall,
    sync_rainfall_recent,
)
from app.services.l07_baseline.service import (
    build_water_body_baselines,
    refresh_weekly,
    water_bodies_with_observations,
)
from app.services.l08_anomaly.service import IndicatorsMissingError, scenes_needing_anomalies
from app.services.l08_anomaly.service import detect_anomalies as _detect_anomalies
from app.services.l08_anomaly.service import process_water_body as _process_anomalies
from app.services.l09_fusion.service import process_water_body as _process_scores
from app.services.l09_fusion.service import scenes_needing_scores
from app.services.l09_fusion.service import score_scene as _score_scene
from app.services.l11_alerts.assembler import assemble_scene as _assemble_scene
from app.services.l12_delivery.brief import generate_brief as _generate_brief
from app.services.l12_delivery.dispatch import DeliveryError
from app.services.l12_delivery.dispatch import dispatch_alert as _dispatch_alert
from app.services.l13_validation.training import retrain as _retrain
from app.services.ops.gauges import refresh_ops_gauges as _refresh_ops_gauges
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
        # Hand cached scenes to L4/L5. `skipped` scenes may have been cached by an
        # earlier run that crashed before masking, so they are checked too.
        to_mask = (
            scenes_needing_mask(session, water_body_id, result.ingested + result.skipped)
            if get_settings().mask_after_ingest
            else []
        )
    for scene_id in to_mask:
        compute_water_mask.delay(water_body_id, scene_id)
    payload = {
        "water_body_id": result.water_body_id,
        "scenes_found": result.scenes_found,
        "ingested": result.ingested,
        "skipped": result.skipped,
        "unusable": result.unusable,
        "failed": result.failed,
        "masks_enqueued": to_mask,
    }
    log.info("ingest done", extra=payload)
    return payload


@celery_app.task(
    bind=True,
    name="app.workers.tasks.compute_water_mask",
    queue="processing",
    autoretry_for=(OSError, ConnectionError, TimeoutError, IngestionMissingError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=4,
    acks_late=True,
)
def compute_water_mask(self: Task, water_body_id: str, scene_id: str) -> dict[str, Any]:
    """L4 + L5 for one cached scene over one water body. Idempotent; retries while
    the ingestion row is still pending (IngestionMissingError) in case of a race."""
    from app.db.models import Scene, WaterBody

    with sync_session() as session:
        wb = session.get(WaterBody, water_body_id)
        scene = session.get(Scene, scene_id)
        if wb is None or scene is None:
            raise LookupError(f"unknown water body {water_body_id!r} or scene {scene_id!r}")
        row, did_work = _compute_water_mask(session, get_store(), wb, scene)
        session.commit()
        # Hand usable scenes to L6. Checked even when this call was a no-op, in case an
        # earlier run crashed between the mask and the indicators.
        enqueue = (
            row.usable
            and get_settings().indicators_after_mask
            and bool(scenes_needing_indicators(session, water_body_id, [scene_id]))
        )
        payload = {
            "water_body_id": water_body_id,
            "scene_id": scene_id,
            "did_work": did_work,
            "usable": row.usable,
            "valid_pixel_pct": row.valid_pixel_pct,
            "water_extent_km2": row.water_extent_km2,
            "chip_key": row.chip_key,
            "indicators_enqueued": enqueue,
        }
    if enqueue:
        compute_indicators.delay(water_body_id, scene_id)
    log.info("water mask done", extra=payload)
    return payload


@celery_app.task(
    bind=True,
    name="app.workers.tasks.compute_indicators",
    queue="processing",
    autoretry_for=(OSError, ConnectionError, TimeoutError, MaskMissingError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=4,
    acks_late=True,
)
def compute_indicators(self: Task, water_body_id: str, scene_id: str) -> dict[str, Any]:
    """L6 for one masked scene over one water body: zonal records into the
    hypertable plus one COG chip per indicator. Idempotent; retries while the
    mask row is still pending (MaskMissingError)."""
    from app.db.models import Scene, WaterBody

    with sync_session() as session:
        wb = session.get(WaterBody, water_body_id)
        scene = session.get(Scene, scene_id)
        if wb is None or scene is None:
            raise LookupError(f"unknown water body {water_body_id!r} or scene {scene_id!r}")
        run, did_work = _compute_indicators(session, get_store(), wb, scene)
        session.commit()
        # Hand finished scenes to L8. Checked even on a no-op so a crash between
        # L6 and L8 is healed by the next call.
        enqueue = (
            run.status == "done"
            and get_settings().anomalies_after_indicators
            and bool(scenes_needing_anomalies(session, water_body_id, [scene_id]))
        )
        payload = {
            "water_body_id": water_body_id,
            "scene_id": scene_id,
            "did_work": did_work,
            "status": run.status,
            "n_zones": run.n_zones,
            "n_observations": run.n_observations,
            "n_rejected": len(run.rejected),
            "chips": {k: v["chip_key"] for k, v in run.chips.items()},
            "anomalies_enqueued": enqueue,
        }
    if enqueue:
        detect_anomalies.delay(water_body_id, scene_id)
    log.info("indicators done", extra=payload)
    return payload


@celery_app.task(
    bind=True,
    name="app.workers.tasks.detect_anomalies",
    queue="processing",
    autoretry_for=(OSError, ConnectionError, TimeoutError, IndicatorsMissingError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=4,
    acks_late=True,
)
def detect_anomalies(self: Task, water_body_id: str, scene_id: str) -> dict[str, Any]:
    """L8 for one L6-processed scene: one anomaly candidate per zone with the
    three detector scores, the rainfall gate decision and a provisional
    severity. Idempotent; retries while the L6 run is still pending."""
    from app.db.models import Scene, WaterBody

    with sync_session() as session:
        wb = session.get(WaterBody, water_body_id)
        scene = session.get(Scene, scene_id)
        if wb is None or scene is None:
            raise LookupError(f"unknown water body {water_body_id!r} or scene {scene_id!r}")
        run, did_work = _detect_anomalies(session, get_store(), wb, scene)
        session.commit()
        enqueue = (
            run.status == "done"
            and get_settings().score_after_anomalies
            and bool(scenes_needing_scores(session, water_body_id, [scene_id]))
        )
        payload = {
            "water_body_id": water_body_id,
            "scene_id": scene_id,
            "did_work": did_work,
            "status": run.status,
            "n_zones": run.n_zones,
            "n_flagged": run.n_flagged,
            "n_alertable": run.n_alertable,
            "n_gated": run.n_gated,
            "spatial_ran": run.spatial_ran,
            "scoring_enqueued": enqueue,
        }
    if enqueue:
        score_candidates.delay(water_body_id, scene_id)
    log.info("anomalies done", extra=payload)
    return payload


@celery_app.task(
    bind=True,
    name="app.workers.tasks.score_candidates",
    queue="scoring",
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def score_candidates(self: Task, water_body_id: str, scene_id: str) -> dict[str, Any]:
    """L9 + L10 for every candidate of one scene: priority score, confidence,
    final severity, four contributions and the summary, under the active model."""
    from app.db.models import Scene, WaterBody

    with sync_session() as session:
        wb = session.get(WaterBody, water_body_id)
        scene = session.get(Scene, scene_id)
        if wb is None or scene is None:
            raise LookupError(f"unknown water body {water_body_id!r} or scene {scene_id!r}")
        scored = _score_scene(session, get_store(), wb, scene)
        session.commit()
        payload = {
            "water_body_id": water_body_id,
            "scene_id": scene_id,
            "n_scored": len(scored),
            "model_version": scored[0].attribution.model_version if scored else None,
            "top": [
                {
                    "zone_id": s.zone_id,
                    "priority": s.priority_score,
                    "severity": s.severity,
                    "confidence": s.confidence,
                }
                for s in sorted(scored, key=lambda s: -s.priority_score)[:3]
            ],
            "alerts_enqueued": bool(scored) and get_settings().alerts_after_scoring,
        }
    if payload["alerts_enqueued"]:
        assemble_alerts.delay(water_body_id, scene_id)
    log.info("scoring done", extra=payload)
    return payload


# --- S8: alerts + delivery ---------------------------------------------------------


@celery_app.task(
    bind=True,
    name="app.workers.tasks.assemble_alerts",
    queue="scoring",
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def assemble_alerts(self: Task, water_body_id: str, scene_id: str) -> dict[str, Any]:
    """L11 for one scored scene: create or update alerts (14-day dedup per
    zone + indicator), then enqueue a brief and a dispatch for every alert that
    is new or escalated."""
    from app.db.models import Scene, WaterBody

    with sync_session() as session:
        wb = session.get(WaterBody, water_body_id)
        scene = session.get(Scene, scene_id)
        if wb is None or scene is None:
            raise LookupError(f"unknown water body {water_body_id!r} or scene {scene_id!r}")
        result = _assemble_scene(session, wb, scene)
        session.commit()
    notify = [(a, "new") for a in result.created] + [(a, "escalated") for a in result.escalated]
    for alert_id, _reason in notify:
        generate_brief.delay(alert_id)
    for alert_id, reason in notify:
        dispatch_alert.delay(alert_id, reason)
    # Updated-but-not-escalated alerts still get a refreshed brief for the latest observation.
    for alert_id in set(result.updated) - set(result.escalated):
        generate_brief.delay(alert_id)
    payload = {
        "water_body_id": water_body_id,
        "scene_id": scene_id,
        "alerts_created": result.created,
        "updated": result.updated,
        "escalated": result.escalated,
        "appended": result.appended,
        "skipped": result.skipped,
    }
    log.info("alerts done", extra=payload)
    return payload


@celery_app.task(
    bind=True,
    name="app.workers.tasks.generate_brief",
    queue="reporting",
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def generate_brief(self: Task, alert_id: str, force: bool = False) -> dict[str, Any]:
    """L12: render the one-page investigation brief to MinIO briefs/{alert_id}.pdf."""
    from app.db.models import Alert

    with sync_session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise LookupError(f"unknown alert {alert_id!r}")
        key, did_work = _generate_brief(session, get_store(), alert, force=force)
        session.commit()
    payload = {"alert_id": alert_id, "brief_key": key, "did_work": did_work}
    log.info("brief done", extra=payload)
    return payload


@celery_app.task(
    bind=True,
    name="app.workers.tasks.dispatch_alert",
    queue="reporting",
    autoretry_for=(DeliveryError, OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=5,
    acks_late=True,
)
def dispatch_alert(self: Task, alert_id: str, reason: str = "new") -> dict[str, Any]:
    """L12: webhook / e-mail delivery to matching recipients (severity threshold +
    jurisdiction). Retries transient failures; DISPATCH_ENABLED=false logs only."""
    from app.db.models import Alert

    with sync_session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise LookupError(f"unknown alert {alert_id!r}")
        try:
            result = _dispatch_alert(session, alert, reason=reason)
        finally:
            session.commit()  # keep the dispatch log even when re-raising for retry
    payload = {
        "alert_id": alert_id,
        "reason": reason,
        "sent": result.sent,
        "skipped": result.skipped,
        "failed": result.failed,
    }
    log.info("dispatch task done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.process_alerts", queue="scoring")
def process_alerts(
    water_body_id: str, date_from: str, date_to: str | None = None, briefs: bool = False
) -> dict[str, Any]:
    """Backfill alerts for every scored scene in [date_from, date_to], oldest
    first so dedup windows behave as they would have in real time. Briefs and
    dispatches are not enqueued unless ``briefs`` is set (backfills should not page anyone)."""
    from app.db.models import WaterBody
    from app.services.l09_fusion.service import anomaly_scenes

    with sync_session() as session:
        wb = session.get(WaterBody, water_body_id)
        if wb is None:
            raise LookupError(f"unknown water body {water_body_id!r}")
        scenes = anomaly_scenes(
            session,
            water_body_id,
            date.fromisoformat(date_from),
            date.fromisoformat(date_to) if date_to else date.fromisoformat(date_from),
        )
        created: list[str] = []
        updated: list[str] = []
        for scene in scenes:
            r = _assemble_scene(session, wb, scene)
            created += r.created
            updated += r.updated
        session.commit()
    if briefs:
        for alert_id in dict.fromkeys(created + updated):
            generate_brief.delay(alert_id)
    payload = {
        "water_body_id": water_body_id,
        "scenes": len(scenes),
        "alerts_created": created,
        "updated": sorted(set(updated)),
    }
    log.info("process alerts done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.process_scores", queue="scoring")
def process_scores(
    water_body_id: str, date_from: str, date_to: str | None = None, force: bool = False
) -> dict[str, Any]:
    """(Re)score every L8-processed scene in [date_from, date_to]; ``force``
    rescores scenes that already have rows, e.g. after activating a new model."""
    with sync_session() as session:
        result = _process_scores(
            session,
            get_store(),
            water_body_id,
            date.fromisoformat(date_from),
            date.fromisoformat(date_to) if date_to else None,
            force=force,
        )
        session.commit()
    payload = {
        "water_body_id": result.water_body_id,
        "scored": result.scored,
        "skipped": result.skipped,
        "failed": result.failed,
    }
    log.info("process scores done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.train_priority_model", queue="scoring")
def train_priority_model() -> dict[str, Any]:
    """Retrain the priority model on validated outcomes (S11). Exits cleanly with
    a logged reason below ``priority_train_min_validations``; a new model is
    promoted only if its held-out precision beats the active model's."""
    with sync_session() as session:
        result = _retrain(session, get_store())
        session.commit()
    payload = result.to_dict()
    log.info("priority model retrain", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.process_anomalies", queue="processing")
def process_anomalies(
    water_body_id: str, date_from: str, date_to: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Backfill anomaly candidates for every L6-processed scene in [date_from, date_to]."""
    with sync_session() as session:
        result = _process_anomalies(
            session,
            get_store(),
            water_body_id,
            date.fromisoformat(date_from),
            date.fromisoformat(date_to) if date_to else None,
            force=force,
        )
        session.commit()
    payload = {
        "water_body_id": result.water_body_id,
        "computed": result.computed,
        "skipped": result.skipped,
        "unusable": result.unusable,
        "failed": result.failed,
    }
    log.info("process anomalies done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.process_indicators", queue="processing")
def process_indicators(
    water_body_id: str, date_from: str, date_to: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Backfill indicators for every masked scene of a body in [date_from, date_to]."""
    with sync_session() as session:
        result = _process_indicators(
            session,
            get_store(),
            water_body_id,
            date.fromisoformat(date_from),
            date.fromisoformat(date_to) if date_to else None,
            force=force,
        )
        session.commit()
    payload = {
        "water_body_id": result.water_body_id,
        "computed": result.computed,
        "unusable": result.unusable,
        "skipped": result.skipped,
        "failed": result.failed,
    }
    log.info("process indicators done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.process_water_body", queue="processing")
def process_water_body(
    water_body_id: str, date_from: str, date_to: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Backfill masks for every ingested scene of a body in [date_from, date_to]."""
    with sync_session() as session:
        result = _process_water_body(
            session,
            get_store(),
            water_body_id,
            date.fromisoformat(date_from),
            date.fromisoformat(date_to) if date_to else None,
            force=force,
        )
        session.commit()
    payload = {
        "water_body_id": result.water_body_id,
        "computed": result.computed,
        "rejected": result.rejected,
        "skipped": result.skipped,
        "failed": result.failed,
    }
    log.info("process done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.poll_tier_scenes", queue="ingestion")
def poll_tier_scenes(tier: int = 1) -> dict[str, Any]:
    """Beat job: find new usable scenes for every body of a tier and enqueue
    their ingestion (Tier 1 every 6 h, Tier 2 daily, Tier 3 weekly)."""
    settings = get_settings()
    lookback = {
        1: settings.ingest_lookback_days,
        2: settings.ingest_lookback_days_tier2,
        3: settings.ingest_lookback_days_tier3,
    }.get(tier, settings.ingest_lookback_days)
    with sync_session() as session:
        pairs = pending_scenes_for_tier(session, tier=tier, lookback_days=lookback)
        session.commit()
    for wb_id, day in pairs:
        ingest_water_body.delay(wb_id, day.isoformat())
    log.info("tier poll", extra={"tier": tier, "enqueued": len(pairs)})
    return {"tier": tier, "enqueued": [[wb, d.isoformat()] for wb, d in pairs]}


@celery_app.task(name="app.workers.tasks.poll_tier1_scenes", queue="ingestion")
def poll_tier1_scenes() -> dict[str, Any]:
    """Kept for callers of the S2 name; same as ``poll_tier_scenes(1)``."""
    result: dict[str, Any] = poll_tier_scenes(1)
    return result


@celery_app.task(name="app.workers.tasks.refresh_ops_gauges", queue="scoring")
def refresh_ops_gauges() -> dict[str, Any]:
    """Beat job: recompute the operational gauges Grafana alerts on."""
    with sync_session() as session:
        snapshot = _refresh_ops_gauges(session)
    log.info("ops gauges refreshed", extra=snapshot)
    return snapshot


# --- S5: baselines + rainfall ------------------------------------------------------


@celery_app.task(
    bind=True,
    name="app.workers.tasks.build_baselines",
    queue="processing",
    autoretry_for=(OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def build_baselines(
    self: Task, water_body_id: str, indicators: list[str] | None = None
) -> dict[str, Any]:
    """Rebuild every (zone, indicator) seasonal baseline of one body from the
    hypertable. Cheap (hundreds of rows per series), so always a full rebuild."""
    with sync_session() as session:
        result = build_water_body_baselines(session, water_body_id, indicators=indicators)
        session.commit()
    payload = {
        "water_body_id": water_body_id,
        "series_built": result.series_built,
        "rows_written": result.rows_written,
        "usable_windows": result.usable_windows,
        "building_windows": result.building_windows,
        "zones": result.zones,
        "history_from": result.history_from.isoformat() if result.history_from else None,
        "history_to": result.history_to.isoformat() if result.history_to else None,
    }
    log.info("baselines done", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.rebuild_all_baselines", queue="processing")
def rebuild_all_baselines(tier: int | None = None) -> dict[str, Any]:
    """Beat job (nightly): refresh the weekly aggregate over the whole archive and
    rebuild baselines for every body that has observations."""
    with sync_session() as session:
        bodies = water_bodies_with_observations(session, tier=tier)
        refresh_weekly(session)
    for wb_id in bodies:
        build_baselines.delay(wb_id)
    log.info("baseline rebuild enqueued", extra={"n": len(bodies)})
    return {"enqueued": bodies}


@celery_app.task(
    bind=True,
    name="app.workers.tasks.sync_rainfall",
    queue="ingestion",
    autoretry_for=(RainfallSourceError, OSError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=5,
    acks_late=True,
)
def sync_rainfall(self: Task, water_body_id: str, backfill: bool = False) -> dict[str, Any]:
    """Pull Open-Meteo precipitation for one body: the lookback window by
    default, or the full archive since ``rainfall_history_start`` when backfilling."""
    with sync_session() as session:
        result = (
            backfill_rainfall(session, water_body_id)
            if backfill
            else sync_rainfall_recent(session, water_body_id)
        )
        session.commit()
    payload = {
        "water_body_id": water_body_id,
        "date_from": result.date_from.isoformat(),
        "date_to": result.date_to.isoformat(),
        "archive_rows": result.archive_rows,
        "forecast_rows": result.forecast_rows,
    }
    log.info("rainfall synced", extra=payload)
    return payload


@celery_app.task(name="app.workers.tasks.sync_rainfall_all", queue="ingestion")
def sync_rainfall_all() -> dict[str, Any]:
    """Beat job (daily): refresh the rainfall lookback window for every registered body."""
    from app.db.models import WaterBody

    with sync_session() as session:
        ids = [str(x) for x in session.scalars(select(WaterBody.id).order_by(WaterBody.tier)).all()]
    for wb_id in ids:
        sync_rainfall.delay(wb_id)
    log.info("rainfall sync enqueued", extra={"n": len(ids)})
    return {"enqueued": ids}


@celery_app.task(name="app.workers.tasks.backfill_history", queue="ingestion")
def backfill_history(
    water_body_id: str, date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    """Bulk historical ingestion for a body: one ingest task per chunk (each
    chains its masks and indicators), plus the rainfall archive. Run
    ``build_baselines`` once the processing queue drains."""
    plan = plan_backfill(
        water_body_id,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
    )
    for start, end in plan.chunks:
        ingest_water_body.delay(water_body_id, start.isoformat(), end.isoformat())
    sync_rainfall.delay(water_body_id, backfill=True)
    payload = {
        "water_body_id": water_body_id,
        "date_from": plan.date_from.isoformat(),
        "date_to": plan.date_to.isoformat(),
        "chunks": plan.n_chunks,
    }
    log.info("backfill enqueued", extra=payload)
    return payload
