"""Validation endpoints (S9 storage, S11 loop): submit with an immediate
verdict, attach a photo, list, and the precision summary that proves the
system works."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import decode_cursor, encode_cursor
from app.core.storage import get_store
from app.db.models import Alert, Validation
from app.db.session import get_session
from app.db.sync_session import sync_session
from app.schemas.jobs import ValidationIn, ValidationList, ValidationOut, ValidationSummary
from app.services.l13_validation.service import (
    precision_summary,
    store_photo,
    submit_validation,
)

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
        alert_severity=v.alert_severity,
        alert_indicator=v.alert_indicator,
        alert_priority_score=v.alert_priority_score,
        alert_observed_on=v.alert_observed_on,
        photo_url=f"/api/v1/validations/{v.id}/photo" if v.photo_key else None,
        created_at=v.created_at,
    )


@router.post("/validations", response_model=ValidationOut, status_code=status.HTTP_201_CREATED)
async def create_validation(
    body: ValidationIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> ValidationOut:
    """Submit a field or lab result against an alert. The verdict is computed
    immediately (matched / not_matched / inconclusive), the alert's status is
    updated, and a field-confirmed normal reading is fed back into the
    seasonal baseline."""
    alert = await session.get(Alert, body.alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"alert {body.alert_id!r} not found")
    result = await session.run_sync(
        lambda s: submit_validation(
            s,
            alert=alert,
            sampled_on=body.sampled_on,
            lab_results=body.lab_results.model_dump(exclude_none=True),
            observed_condition=body.observed_condition,
            notes=body.notes,
            submitted_by=body.submitted_by,
        )
    )
    await session.commit()
    await session.refresh(result.validation)
    return _out(result.validation)


@router.get("/validations/summary", response_model=ValidationSummary)
async def validation_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ValidationSummary:
    """Precision to date, overall and by indicator / severity band."""
    data: dict[str, Any] = await session.run_sync(precision_summary)
    return ValidationSummary.model_validate(data)


@router.get("/validations", response_model=ValidationList)
async def list_validations(
    session: Annotated[AsyncSession, Depends(get_session)],
    alert_id: str | None = None,
    verdict: Annotated[str | None, Query(pattern="^(matched|not_matched|inconclusive)$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> ValidationList:
    """Submitted validations, newest first, with their verdict."""
    stmt = select(Validation)
    if alert_id:
        stmt = stmt.where(Validation.alert_id == alert_id)
    if verdict:
        stmt = stmt.where(Validation.verdict == verdict)
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


@router.get("/validations/{validation_id}", response_model=ValidationOut)
async def get_validation(
    validation_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> ValidationOut:
    v = await session.get(Validation, validation_id)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"validation {validation_id} not found")
    return _out(v)


def _put_photo(validation_id: int, data: bytes, content_type: str, filename: str) -> str:
    with sync_session() as session:
        v = session.get(Validation, validation_id)
        if v is None:
            raise LookupError(str(validation_id))
        key = store_photo(
            session, get_store(), v, data=data, content_type=content_type, filename=filename
        )
        session.commit()
    return key


@router.post("/validations/{validation_id}/photo", response_model=ValidationOut)
async def upload_photo(
    validation_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP, up to 15 MB")],
) -> ValidationOut:
    """Attach a site photo to a validation (stored in MinIO)."""
    data = await file.read()
    try:
        await run_in_threadpool(
            _put_photo,
            validation_id,
            data,
            file.content_type or "application/octet-stream",
            file.filename or "photo",
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"validation {exc} not found") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    v = await session.get(Validation, validation_id)
    assert v is not None
    await session.refresh(v)
    return _out(v)


def _get_photo(validation_id: int) -> tuple[bytes, str]:
    with sync_session() as session:
        v = session.get(Validation, validation_id)
        if v is None or not v.photo_key:
            raise LookupError(str(validation_id))
        key = v.photo_key
    data = get_store().get_bytes(key)
    ext = key.rsplit(".", 1)[-1].lower()
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    return data, media.get(ext, "application/octet-stream")


@router.get("/validations/{validation_id}/photo", response_class=Response)
async def get_photo(validation_id: int) -> Response:
    try:
        data, media = await run_in_threadpool(_get_photo, validation_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no photo for this validation") from None
    return Response(
        content=data, media_type=media, headers={"Cache-Control": "private, max-age=3600"}
    )
