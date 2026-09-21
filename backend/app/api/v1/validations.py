"""Validation endpoints (S9 storage). S11 adds the verdict, the photo upload
and the precision summary; the request/response shapes are fixed here."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import decode_cursor, encode_cursor
from app.db.models import Alert, Validation
from app.db.session import get_session
from app.schemas.jobs import ValidationIn, ValidationList, ValidationOut

router = APIRouter(tags=["validations"])


def _out(v: Validation) -> ValidationOut:
    return ValidationOut(
        id=v.id,
        alert_id=v.alert_id,
        sampled_on=v.sampled_on,
        lab_results=v.lab_results,
        observed_condition=v.observed_condition,
        notes=v.notes,
        submitted_by=v.submitted_by,
        verdict=v.verdict,
        verdict_reason=v.verdict_reason,
        created_at=v.created_at,
    )


@router.post("/validations", response_model=ValidationOut, status_code=status.HTTP_201_CREATED)
async def create_validation(
    body: ValidationIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> ValidationOut:
    """Submit a field or lab result against an alert. The verdict (matched /
    not_matched / inconclusive) is computed by the validation loop (S11)."""
    alert = await session.get(Alert, body.alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"alert {body.alert_id!r} not found")
    row = Validation(
        alert_id=body.alert_id,
        sampled_on=body.sampled_on,
        lab_results=body.lab_results.model_dump(exclude_none=True),
        observed_condition=body.observed_condition,
        notes=body.notes,
        submitted_by=body.submitted_by,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.get("/validations", response_model=ValidationList)
async def list_validations(
    session: Annotated[AsyncSession, Depends(get_session)],
    alert_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> ValidationList:
    """Submitted validations, newest first, with their verdict when computed."""
    stmt = select(Validation)
    if alert_id:
        stmt = stmt.where(Validation.alert_id == alert_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    after = decode_cursor(cursor, 2)
    if after:
        a_ts, a_id = datetime.fromisoformat(after[0]), int(after[1])
        stmt = stmt.where(
            (Validation.created_at < a_ts)
            | ((Validation.created_at == a_ts) & (Validation.id > a_id))
        )
    rows = list(
        (
            await session.execute(
                stmt.order_by(Validation.created_at.desc(), Validation.id).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1].created_at.isoformat(), rows[-1].id) if more and rows else None
    return ValidationList(items=[_out(v) for v in rows], total=int(total), next_cursor=nxt)
