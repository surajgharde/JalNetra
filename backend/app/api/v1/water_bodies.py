"""Water body endpoints (S9): list, detail, observations, indicators, series.
Series and indicators are Redis-cached for 5 minutes under a key that includes
the body's latest scene id, so fresh data is never served stale."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import decode_cursor, encode_cursor
from app.core.cache import cache_key, cached_json
from app.core.config import Settings, get_settings
from app.db.models import WaterBody, Zone
from app.db.session import get_session
from app.schemas.water_bodies import (
    IndicatorsResponse,
    ObservationList,
    SeriesResponse,
    WaterBodyDetail,
    WaterBodyList,
)
from app.services.l02_api import water_bodies as q
from app.services.l06_indicators.registry import INDICATORS

router = APIRouter(tags=["water-bodies"])

IndicatorKey = Annotated[str, Query(description=f"one of {', '.join(INDICATORS)}")]


async def _body(session: AsyncSession, water_body_id: str) -> WaterBody:
    wb = await session.get(WaterBody, water_body_id)
    if wb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"water body {water_body_id!r} not found")
    return wb


@router.get("/water-bodies", response_model=WaterBodyList)
async def list_water_bodies(
    session: Annotated[AsyncSession, Depends(get_session)],
    district: str | None = None,
    tier: Annotated[int | None, Query(ge=1, le=3)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> WaterBodyList:
    """Registered water bodies with tier, area, latest observation and status."""
    after = decode_cursor(cursor, 3)
    items, total, nxt = await session.run_sync(
        lambda s: q.list_bodies(s, district=district, tier=tier, limit=limit, after=after)
    )
    return WaterBodyList(
        items=items, total=total, next_cursor=None if nxt is None else encode_cursor(*nxt)
    )


@router.get("/water-bodies/{water_body_id}", response_model=WaterBodyDetail)
async def get_water_body(
    water_body_id: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> WaterBodyDetail:
    """Detail with the GeoJSON boundary and zone polygons."""
    wb = await _body(session, water_body_id)
    detail = await session.run_sync(lambda s: q.body_detail(s, wb))
    return WaterBodyDetail.model_validate(detail)


@router.get("/water-bodies/{water_body_id}/observations", response_model=ObservationList)
async def list_observations(
    water_body_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> ObservationList:
    """Available dates with cloud cover, usability and the stage each scene reached."""
    await _body(session, water_body_id)
    after = decode_cursor(cursor, 2)
    items, total, nxt = await session.run_sync(
        lambda s: q.list_observations(
            s, water_body_id, date_from=date_from, date_to=date_to, limit=limit, after=after
        )
    )
    return ObservationList(
        water_body_id=water_body_id,
        items=items,
        total=total,
        next_cursor=None if nxt is None else encode_cursor(*nxt),
    )


@router.get("/water-bodies/{water_body_id}/indicators", response_model=IndicatorsResponse)
async def get_indicators(
    water_body_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    on: Annotated[
        date | None,
        Query(alias="date", description="latest scene on or before this date; default today"),
    ] = None,
    zone: str | None = None,
) -> IndicatorsResponse:
    """Indicator values for the latest scene on/before ``date``, with the
    DOY-matched baseline and the deviation, per zone."""
    await _body(session, water_body_id)
    if zone is not None and (await session.get(Zone, zone)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"zone {zone!r} not found")
    requested = on or datetime.now(UTC).date()
    marker = await session.run_sync(lambda s: q.latest_scene_marker(s, water_body_id))
    key = cache_key("indicators", water_body_id, requested, zone, marker)

    async def produce() -> dict[str, Any]:
        data = await session.run_sync(
            lambda s: q.zone_indicators(s, water_body_id, requested, zone, settings=settings)
        )
        return IndicatorsResponse.model_validate(data).model_dump(mode="json")

    payload, hit = await cached_json(key, produce, settings=settings)
    payload["cached"] = hit
    return IndicatorsResponse.model_validate(payload)


@router.get("/water-bodies/{water_body_id}/series", response_model=SeriesResponse)
async def get_series(
    water_body_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    indicator: IndicatorKey = "ndti_turbidity",
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    zone: Annotated[str | None, Query(description="defaults to the body's first zone")] = None,
) -> SeriesResponse:
    """Zone time series for charting, each point with its seasonal band and z."""
    await _body(session, water_body_id)
    if indicator not in INDICATORS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown indicator {indicator!r}"
        )
    zone_id = zone or await session.run_sync(lambda s: q.first_zone_id(s, water_body_id))
    if zone_id is None or (await session.get(Zone, zone_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"zone {zone!r} not found")
    to = date_to or datetime.now(UTC).date()
    frm = date_from or (to - timedelta(days=365))
    if frm > to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from is after to")
    marker = await session.run_sync(lambda s: q.latest_scene_marker(s, water_body_id))
    key = cache_key("series", water_body_id, zone_id, indicator, frm, to, marker)

    async def produce() -> dict[str, Any]:
        data = await session.run_sync(
            lambda s: q.series(s, water_body_id, zone_id, indicator, frm, to, settings=settings)
        )
        return SeriesResponse.model_validate(data).model_dump(mode="json")

    payload, hit = await cached_json(key, produce, settings=settings)
    payload["cached"] = hit
    return SeriesResponse.model_validate(payload)
