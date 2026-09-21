"""Pipeline job endpoints (S9): trigger ingestion, watch it progress."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.pagination import decode_cursor, encode_cursor
from app.db.models import Job
from app.db.session import get_session
from app.schemas.jobs import IngestJobRequest, JobList, JobOut
from app.services.l02_api import jobs as q

router = APIRouter(tags=["jobs"])


def _view_of(job: Job) -> Callable[[Session], dict[str, Any]]:
    return lambda s: q.job_view(s, job)


@router.post("/jobs/ingest", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_ingest_job(
    body: IngestJobRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> JobOut:
    """Enqueue ingestion for a water body and date range. Each ingested scene
    chains mask -> indicators -> anomalies -> scoring -> alerts on its own;
    poll ``GET /jobs/{id}`` for the live readout."""
    from app.workers.tasks import backfill_history, ingest_water_body

    date_to = body.date_to or body.date_from
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
                requested_by=body.requested_by,
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
            body.water_body_id, body.date_from.isoformat(), date_to.isoformat()
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
