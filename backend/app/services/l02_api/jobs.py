"""Pipeline jobs (S9): create, and derive live progress from the stage tables.

Progress is computed, not reported: for every usable scene of the body in the
job window we count which stages have finished. A scene the mask rejected
still "finishes" the downstream stages (L6/L8 write ``skipped`` runs), so a
cloudy week reads as 100 % done rather than stuck at 40 %.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    Alert,
    AnomalyRun,
    CandidateScore,
    IndicatorRun,
    Job,
    Scene,
    SceneIngestion,
    WaterBody,
    WaterMaskRecord,
)

STAGES: tuple[str, ...] = ("ingestion", "mask", "indicators", "anomalies", "scoring")
BACKFILL_THRESHOLD_DAYS = 62  # longer windows go through the chunked backfill task


@dataclass(frozen=True)
class StageCount:
    stage: str
    done: int
    total: int

    @property
    def pct(self) -> float:
        return round(100.0 * self.done / self.total, 1) if self.total else 0.0


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def create_job(
    session: Session,
    *,
    kind: str,
    water_body_id: str,
    date_from: date,
    date_to: date,
    requested_by: str | None,
) -> Job:
    if session.get(WaterBody, water_body_id) is None:
        raise LookupError(f"unknown water body {water_body_id!r}")
    if date_to < date_from:
        raise ValueError("date_to is before date_from")
    job = Job(
        id=new_job_id(),
        kind=kind,
        water_body_id=water_body_id,
        date_from=date_from,
        date_to=date_to,
        status="queued",
        requested_by=requested_by,
        snapshot={},
    )
    session.add(job)
    session.flush()
    return job


def _window(job: Job) -> tuple[datetime, datetime]:
    start = datetime(job.date_from.year, job.date_from.month, job.date_from.day, tzinfo=UTC)
    end = datetime(job.date_to.year, job.date_to.month, job.date_to.day, tzinfo=UTC) + timedelta(
        days=1
    )
    return start, end


def _scene_ids(session: Session, job: Job) -> tuple[list[str], list[str]]:
    """(all scenes on the body's tiles in the window, the usable subset)."""
    wb = session.get(WaterBody, job.water_body_id)
    assert wb is not None
    start, end = _window(job)
    rows = session.execute(
        select(Scene.id, Scene.usable).where(
            Scene.mgrs_tile.in_(list(wb.mgrs_tiles or [])),
            Scene.sensed_at >= start,
            Scene.sensed_at < end,
        )
    ).all()
    return [r[0] for r in rows], [r[0] for r in rows if r[1]]


def _count(session: Session, col: Any, *where: Any) -> int:
    return int(session.execute(select(func.count(func.distinct(col))).where(*where)).scalar_one())


def stage_counts(session: Session, job: Job) -> tuple[list[StageCount], int, int, int]:
    """Per-stage (done, total) plus scenes_found, scenes_usable, alerts_created."""
    all_ids, usable = _scene_ids(session, job)
    n = len(usable)
    wb = job.water_body_id
    if not usable:
        return [StageCount(s, 0, 0) for s in STAGES], len(all_ids), 0, 0
    ingested = _count(
        session,
        SceneIngestion.scene_id,
        SceneIngestion.water_body_id == wb,
        SceneIngestion.scene_id.in_(usable),
        SceneIngestion.status == "done",
    )
    masked = _count(
        session,
        WaterMaskRecord.scene_id,
        WaterMaskRecord.water_body_id == wb,
        WaterMaskRecord.scene_id.in_(usable),
        WaterMaskRecord.status == "done",
    )
    # A mask that is too cloudy over the body ends the pipeline for that scene
    # (L6 is never enqueued), so it counts as finished for every later stage.
    mask_unusable = _count(
        session,
        WaterMaskRecord.scene_id,
        WaterMaskRecord.water_body_id == wb,
        WaterMaskRecord.scene_id.in_(usable),
        WaterMaskRecord.status == "done",
        WaterMaskRecord.usable.is_(False),
    )
    indicators = _count(
        session,
        IndicatorRun.scene_id,
        IndicatorRun.water_body_id == wb,
        IndicatorRun.scene_id.in_(usable),
        IndicatorRun.status.in_(("done", "skipped")),
    )
    anomalies = _count(
        session,
        AnomalyRun.scene_id,
        AnomalyRun.water_body_id == wb,
        AnomalyRun.scene_id.in_(usable),
        AnomalyRun.status.in_(("done", "skipped")),
    )
    skipped_anom = _count(
        session,
        AnomalyRun.scene_id,
        AnomalyRun.water_body_id == wb,
        AnomalyRun.scene_id.in_(usable),
        AnomalyRun.status == "skipped",
    )
    scored = _count(
        session,
        CandidateScore.scene_id,
        CandidateScore.water_body_id == wb,
        CandidateScore.scene_id.in_(usable),
    )
    start, end = _window(job)
    alerts = _count(
        session,
        Alert.id,
        Alert.water_body_id == wb,
        Alert.first_observed_at >= start,
        Alert.first_observed_at < end,
    )
    counts = [
        StageCount("ingestion", min(ingested, n), n),
        StageCount("mask", min(masked, n), n),
        StageCount("indicators", min(indicators + mask_unusable, n), n),
        StageCount("anomalies", min(anomalies + mask_unusable, n), n),
        StageCount("scoring", min(scored + skipped_anom + mask_unusable, n), n),
    ]
    return counts, len(all_ids), n, alerts


def celery_state(task_id: str | None) -> str | None:
    if not task_id:
        return None
    from app.workers.celery_app import celery_app

    try:
        return str(celery_app.AsyncResult(task_id).state)
    except Exception:
        return None


def job_view(session: Session, job: Job, *, settings: Settings | None = None) -> dict[str, Any]:
    """Derive status + progress and persist a snapshot on the row."""
    settings = settings or get_settings()
    counts, found, usable, alerts = stage_counts(session, job)
    state = celery_state(job.celery_task_id)
    progress = round(sum(c.pct for c in counts) / len(counts), 1) if usable else 0.0
    current = next((c.stage for c in counts if c.done < c.total), None)

    if job.status not in ("done", "failed"):
        if state == "FAILURE":
            job.status = "failed"
            job.error = job.error or "ingestion task failed; see worker logs"
            job.finished_at = job.finished_at or datetime.now(UTC)
        elif usable and current is None:
            job.status = "done"
            job.finished_at = job.finished_at or datetime.now(UTC)
        elif state == "SUCCESS" and found == 0:
            job.status = "done"  # searched, nothing to process
            job.finished_at = job.finished_at or datetime.now(UTC)
        elif state in ("STARTED", "RETRY") or usable:
            job.status = "running"
    view = {
        "job_id": job.id,
        "kind": job.kind,
        "water_body_id": job.water_body_id,
        "date_from": job.date_from,
        "date_to": job.date_to,
        "status": job.status,
        "progress_pct": 100.0 if job.status == "done" else progress,
        "current_stage": None
        if job.status == "done"
        else (current or ("ingestion" if not usable else None)),
        "stages": [
            {"stage": c.stage, "done": c.done, "total": c.total, "pct": c.pct} for c in counts
        ],
        "scenes_found": found,
        "scenes_usable": usable,
        "alerts_created": alerts,
        "celery_state": state,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }
    job.snapshot = {
        "progress_pct": view["progress_pct"],
        "current_stage": view["current_stage"],
        "scenes_found": found,
        "scenes_usable": usable,
        "alerts_created": alerts,
    }
    session.flush()
    return view


def list_jobs(
    session: Session, *, water_body_id: str | None, limit: int, after: list[Any] | None
) -> tuple[list[Job], list[Any] | None]:
    stmt = select(Job)
    if water_body_id:
        stmt = stmt.where(Job.water_body_id == water_body_id)
    if after:
        a_ts, a_id = datetime.fromisoformat(after[0]), after[1]
        stmt = stmt.where((Job.created_at < a_ts) | ((Job.created_at == a_ts) & (Job.id > a_id)))
    rows = list(
        session.scalars(stmt.order_by(Job.created_at.desc(), Job.id).limit(limit + 1)).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = [rows[-1].created_at.isoformat(), rows[-1].id] if more and rows else None
    return rows, cursor
