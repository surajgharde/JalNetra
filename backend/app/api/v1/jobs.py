"""Pipeline job endpoints (S9): trigger ingestion, watch it progress."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.pagination import decode_cursor, encode_cursor
from app.core.auth import ActorDep
from app.core.config import Settings, get_settings
from app.core.storage import get_store
from app.db.models import Job
from app.db.session import get_session
from app.db.sync_session import sync_session
from app.schemas.jobs import IngestJobRequest, JobList, JobOut
from app.services.l02_api import jobs as q

router = APIRouter(tags=["jobs"])


def _view_of(job: Job) -> Callable[[Session], dict[str, Any]]:
    return lambda s: q.job_view(s, job)


@router.post("/jobs/ingest", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_ingest_job(
    body: IngestJobRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    actor: ActorDep,
) -> JobOut:
    """Enqueue ingestion for a water body and date range. Each ingested scene
    chains mask -> indicators -> anomalies -> scoring -> alerts on its own;
    poll ``GET /jobs/{id}`` for the live readout. Requires ``X-API-Key``.

    The window is capped at ``job_max_span_days`` and a job that is already
    queued or running for the same body and window is returned instead of
    being enqueued twice."""
    from app.workers.tasks import backfill_history, ingest_water_body

    date_to = body.date_to or body.date_from
    if (date_to - body.date_from).days > settings.job_max_span_days:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"window wider than {settings.job_max_span_days} days; split the backfill",
        )
    existing = await session.run_sync(
        lambda s: q.find_active_job(s, body.water_body_id, body.date_from, date_to)
    )
    if existing is not None:
        view = await session.run_sync(lambda s: q.job_view(s, existing))
        await session.commit()
        return JobOut.model_validate(view)
    requested_by = actor.label(body.requested_by)
    try:
        job = await session.run_sync(
            lambda s: q.create_job(
                s,
                kind="backfill"
                if (date_to - body.date_from).days > q.BACKFILL_THRESHOLD_DAYS
                else "ingest",
                water_body_id=body.water_body_id,
                date_from=body.date_from,
                date_to=date_to,
                requested_by=requested_by,
            )
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if job.kind == "backfill":
        result = backfill_history.delay(
            body.water_body_id, body.date_from.isoformat(), date_to.isoformat()
        )
    else:
        result = ingest_water_body.delay(
            body.water_body_id,
            body.date_from.isoformat(),
            date_to.isoformat(),
            max_scenes=body.max_scenes,
        )
    job.celery_task_id = result.id
    view = await session.run_sync(lambda s: q.job_view(s, job))
    await session.commit()
    return JobOut.model_validate(view)


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    session: Annotated[AsyncSession, Depends(get_session)],
    water_body_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> JobList:
    after = decode_cursor(cursor, 2)
    rows, nxt = await session.run_sync(
        lambda s: q.list_jobs(s, water_body_id=water_body_id, limit=limit, after=after)
    )
    views = []
    for job in rows:
        views.append(await session.run_sync(_view_of(job)))
    await session.commit()
    return JobList(
        items=[JobOut.model_validate(v) for v in views],
        next_cursor=None if nxt is None else encode_cursor(*nxt),
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> JobOut:
    """State, progress percent and the stage currently in flight."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id!r} not found")
    view = await session.run_sync(lambda s: q.job_view(s, job))
    await session.commit()
    return JobOut.model_validate(view)


def _report_bytes(job_id: str, fmt: str, refresh: bool) -> bytes:
    """Sync: collect + render (or reuse) the run report. Runs in the threadpool."""
    from app.services.l12_delivery.job_report import build_report

    store = get_store()
    with sync_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(job_id)
        q.job_view(session, job)  # refresh status first so a finished job gets its stored copy
        blob = build_report(session, store, job, fmt, refresh=refresh)
        session.commit()
    return blob


@router.get(
    "/jobs/{job_id}/report.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def job_report_pdf(job_id: str, refresh: bool = False, download: bool = True) -> Response:
    """The analysis report for a pipeline run: a summary page (every day in the
    window, trends, alerts) then one page per observed day with that day's
    satellite image, detected water, turbidity and chlorophyll rasters and the
    per-zone data. Rendered live while the job runs; stored once it is done
    (``refresh=true`` re-renders)."""
    try:
        pdf = await run_in_threadpool(_report_bytes, job_id, "pdf", refresh)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {exc} not found") from exc
    disposition = "attachment" if download else "inline"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="jalnetra-{job_id}.pdf"'},
    )


@router.get(
    "/jobs/{job_id}/report.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def job_report_csv(job_id: str, refresh: bool = False) -> Response:
    """The same run as data: one row per (day, zone) with scene, coverage, water
    extent, rainfall, every indicator mean, baseline z-scores, anomaly and
    priority fields, and the alert id when one was raised."""
    try:
        blob = await run_in_threadpool(_report_bytes, job_id, "csv", refresh)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {exc} not found") from exc
    return Response(
        content=blob,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="jalnetra-{job_id}.csv"'},
    )
