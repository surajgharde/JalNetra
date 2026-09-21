"""Alert endpoints (S8): list, detail, GeoJSON feed, geometry, brief PDF,
status workflow. S9 adds the water-body, series and job endpoints.

Every response carries ``disclaimer`` verbatim; the frontend renders it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.storage import get_store
from app.db.models import Alert, WaterBody, Zone
from app.db.session import get_session
from app.db.sync_session import sync_session
from app.schemas.alerts import (
    AlertList,
    AlertOut,
    AlertStatusUpdate,
    GeoJSONFeatureCollection,
)
from app.services.l10_explain.explain import DISCLAIMER
from app.services.l11_alerts.assembler import set_status
from app.services.l11_alerts.payload import alert_list, alert_out, alerts_feature_collection
from app.services.l12_delivery.brief import generate_brief

router = APIRouter(tags=["alerts"])

StatusFilter = Literal["open", "investigating", "validated", "dismissed", "active", "all"]
SeverityFilter = Literal["low", "medium", "high"]

_SEVERITY_AT_LEAST = {
    "low": ("low", "medium", "high"),
    "medium": ("medium", "high"),
    "high": ("high",),
}


def _base_query(
    status_filter: StatusFilter,
    severity: SeverityFilter | None,
    min_priority: float,
    water_body_id: str | None,
    district: str | None,
) -> Select[tuple[Alert, WaterBody, Zone]]:
    stmt = (
        select(Alert, WaterBody, Zone)
        .join(WaterBody, WaterBody.id == Alert.water_body_id)
        .join(Zone, Zone.id == Alert.zone_id)
    )
    if status_filter == "active":
        stmt = stmt.where(Alert.status.in_(("open", "investigating")))
    elif status_filter != "all":
        stmt = stmt.where(Alert.status == status_filter)
    if severity:
        stmt = stmt.where(Alert.severity.in_(_SEVERITY_AT_LEAST[severity]))
    if min_priority > 0:
        stmt = stmt.where(Alert.priority_score >= min_priority)
    if water_body_id:
        stmt = stmt.where(Alert.water_body_id == water_body_id)
    if district:
        stmt = stmt.where(WaterBody.district == district)
    return stmt


@router.get("/alerts", response_model=AlertList)
async def list_alerts(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[StatusFilter, Query(alias="status")] = "active",
    severity: Annotated[SeverityFilter | None, Query(description="minimum severity")] = None,
    min_priority: Annotated[float, Query(ge=0, le=100)] = 0,
    water_body_id: str | None = None,
    district: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlertList:
    """Alert list, priority descending."""
    stmt = _base_query(status_filter, severity, min_priority, water_body_id, district)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(Alert.priority_score.desc(), Alert.last_observed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return alert_list([(a, wb, z) for a, wb, z in rows], int(total))


@router.get("/alerts.geojson", response_model=GeoJSONFeatureCollection)
async def alerts_geojson(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[StatusFilter, Query(alias="status")] = "active",
    severity: Annotated[SeverityFilter | None, Query()] = None,
    min_priority: Annotated[float, Query(ge=0, le=100)] = 0,
    water_body_id: str | None = None,
    district: str | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
) -> GeoJSONFeatureCollection:
    """Open alerts as a FeatureCollection; ``priority_score`` in properties drives map styling."""
    stmt = _base_query(status_filter, severity, min_priority, water_body_id, district)
    rows = (await session.execute(stmt.order_by(Alert.priority_score.desc()).limit(limit))).all()
    return alerts_feature_collection([(a, wb, z) for a, wb, z in rows])


async def _load(session: AsyncSession, alert_id: str) -> tuple[Alert, WaterBody, Zone]:
    row = (
        await session.execute(
            select(Alert, WaterBody, Zone)
            .join(WaterBody, WaterBody.id == Alert.water_body_id)
            .join(Zone, Zone.id == Alert.zone_id)
            .where(Alert.id == alert_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"alert {alert_id!r} not found")
    return row[0], row[1], row[2]


@router.get("/alerts/{alert_id}", response_model=AlertOut)
async def get_alert(
    alert_id: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> AlertOut:
    alert, wb, zone = await _load(session, alert_id)
    return alert_out(alert, wb, zone)


@router.get("/alerts/{alert_id}/geometry.geojson")
async def get_alert_geometry(
    alert_id: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, object]:
    """The flagged area (plume polygon, or the zone when no plume was drawn)."""
    alert, _wb, _zone = await _load(session, alert_id)
    return {
        "type": "Feature",
        "id": alert.id,
        "geometry": mapping(to_shape(alert.geom)),
        "properties": {
            "alert_id": alert.id,
            "affected_area_km2": alert.affected_area_km2,
            "is_plume": alert.affected_area_km2 is not None,
            "disclaimer": DISCLAIMER,
        },
    }


def _brief_bytes(alert_id: str, force: bool) -> bytes:
    """Sync: render (or reuse) the brief. Runs in the threadpool."""
    store = get_store()
    with sync_session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise LookupError(alert_id)
        key, _ = generate_brief(session, store, alert, force=force)
        session.commit()
    return store.get_bytes(key)


@router.get(
    "/alerts/{alert_id}/brief.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_brief(
    alert_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    force: bool = False,
) -> Response:
    """The one-page investigation brief. Generated on first request if the
    worker has not produced it yet; ``force=true`` re-renders."""
    await _load(session, alert_id)
    try:
        pdf = await run_in_threadpool(_brief_bytes, alert_id, force)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{alert_id}.pdf"'},
    )


@router.patch("/alerts/{alert_id}/status", response_model=AlertOut)
async def update_status(
    alert_id: str,
    body: AlertStatusUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertOut:
    """Field workflow: open -> investigating -> validated | dismissed (any order is allowed;
    the change is audited with who and why)."""
    alert, wb, zone = await _load(session, alert_id)
    await session.run_sync(lambda s: set_status(s, alert, body.status, by=body.by, note=body.note))
    await session.commit()
    await session.refresh(alert)
    return alert_out(alert, wb, zone)


@router.get("/alerts/{alert_id}/webhook-preview")
async def webhook_preview(
    alert_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Exactly what a webhook recipient receives (dry run; nothing is sent)."""
    alert, wb, zone = await _load(session, alert_id)
    return {
        "dispatch_enabled": settings.dispatch_enabled,
        "body": alert_out(alert, wb, zone).model_dump(mode="json"),
    }
