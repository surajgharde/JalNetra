"""Water body endpoints (S9): list, detail, observations, indicators, series.
Series and indicators are Redis-cached for 5 minutes under a key that includes
the body's latest scene id, so fresh data is never served stale."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import decode_cursor, encode_cursor
from app.core.auth import ActorDep
from app.core.cache import cache_key, cached_json
from app.core.config import Settings, get_settings
from app.db.models import WaterBody, Zone
from app.db.session import get_session
from app.schemas.discovery import (
    DiscoveredWaterBodyOut,
    DiscoverAtPointRequest,
    DiscoverAtPointResponse,
    ImportDynamicRequest,
    ImportDynamicResponse,
    PlaceSuggestResponse,
    PlaceSuggestionOut,
    SearchAndDiscoverRequest,
    SearchAndDiscoverResponse,
)
from app.schemas.water_bodies import (
    IndicatorsResponse,
    ObservationList,
    SeriesResponse,
    WaterBodyDetail,
    WaterBodyList,
)
from app.services.l02_api import discovery as dq
from app.services.l02_api import water_bodies as q
from app.services.l06_indicators.registry import INDICATORS
from app.services.registry import discovery as disc

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


@router.get("/water-bodies/place-suggestions", response_model=PlaceSuggestResponse)
async def place_suggestions(
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
) -> PlaceSuggestResponse:
    """Live autocomplete for the search bar: up to ``limit`` India-only place
    or water-body name matches, cached briefly since the same partial query is
    refetched on nearly every keystroke while the user is typing.

    Registered ahead of ``/water-bodies/{water_body_id}`` -- a literal path
    segment must win over the id parameter, or "place-suggestions" would be
    looked up as if it were a water body id."""
    query_norm = q.strip()
    if not query_norm:
        return PlaceSuggestResponse(query=q, items=[])

    async def produce() -> list[dict[str, Any]]:
        results = await disc.geocode_suggestions(query_norm, settings, limit=limit)
        return [dataclasses.asdict(r) for r in results]

    try:
        items_raw, _ = await cached_json(
            cache_key("place-suggest", query_norm.lower(), limit),
            produce,
            settings=settings,
            ttl_s=settings.discovery_cache_ttl_s,
        )
    except disc.DiscoveryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return PlaceSuggestResponse(query=q, items=[PlaceSuggestionOut(**it) for it in items_raw])


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


@router.post("/water-bodies/discover", response_model=DiscoverAtPointResponse)
async def discover_at_point(
    body: DiscoverAtPointRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DiscoverAtPointResponse:
    """Scan directly around a coordinate the caller already resolved -- e.g. the
    autocomplete's own lat/lon -- skipping Nominatim entirely. Shares its
    Overpass cache key with ``search-and-discover`` so picking a suggestion
    right after typing it never double-queries Overpass for the same point."""
    radius_km = settings.discovery_default_radius_km if body.radius_km is None else body.radius_km
    if not (settings.discovery_min_radius_km <= radius_km <= settings.discovery_max_radius_km):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"radius_km must be between {settings.discovery_min_radius_km} and "
            f"{settings.discovery_max_radius_km}",
        )
    lat, lon = body.latitude, body.longitude

    async def scan_produce() -> list[dict[str, Any]]:
        found = await disc.scan_water_bodies(lat, lon, radius_km, settings)
        return [dataclasses.asdict(d) for d in found]

    try:
        items_raw, _ = await cached_json(
            cache_key("discover-scan", round(lat, 4), round(lon, 4), radius_km),
            scan_produce,
            settings=settings,
            ttl_s=settings.discovery_cache_ttl_s,
        )
    except disc.DiscoveryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    overlaps = await session.run_sync(
        lambda s: dq.annotate_overlaps(s, [(it["osm_id"], it["geometry"]) for it in items_raw])
    )
    items = [
        DiscoveredWaterBodyOut(**it, already_registered_id=overlaps.get(it["osm_id"]))
        for it in items_raw
    ]
    return DiscoverAtPointResponse(centre=[lon, lat], radius_km=radius_km, items=items, total=len(items))


@router.post("/water-bodies/search-and-discover", response_model=SearchAndDiscoverResponse)
async def search_and_discover(
    body: SearchAndDiscoverRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchAndDiscoverResponse:
    """Resolve a place or water body name (e.g. "Nagpur", "Ambazari Lake") and
    list every OSM water polygon within ``radius_km`` of it, largest first.

    Read-only: nothing is written to the registry until a result is posted to
    ``/water-bodies/import-dynamic``. The geocode and the OSM scan are each
    cached for a day (both are calls to shared public services with usage
    limits); which candidates already overlap a registered water body is
    checked fresh every time, since that reflects live registry state.
    """
    query_norm = body.query.strip()
    radius_km = settings.discovery_default_radius_km if body.radius_km is None else body.radius_km
    if not (settings.discovery_min_radius_km <= radius_km <= settings.discovery_max_radius_km):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"radius_km must be between {settings.discovery_min_radius_km} and "
            f"{settings.discovery_max_radius_km}",
        )

    async def geocode_produce() -> dict[str, Any]:
        result = await disc.geocode_place(query_norm, settings)
        return {
            "lat": result.lat,
            "lon": result.lon,
            "display_name": result.display_name,
            "district": result.district,
            "state": result.state,
        }

    try:
        geo, _ = await cached_json(
            cache_key("geocode", query_norm.lower()),
            geocode_produce,
            settings=settings,
            ttl_s=settings.discovery_cache_ttl_s,
        )
    except disc.PlaceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"location {exc} not found") from exc
    except disc.DiscoveryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    lat, lon = float(geo["lat"]), float(geo["lon"])

    async def scan_produce() -> list[dict[str, Any]]:
        found = await disc.scan_water_bodies(lat, lon, radius_km, settings)
        return [dataclasses.asdict(d) for d in found]

    try:
        items_raw, _ = await cached_json(
            cache_key("discover-scan", round(lat, 4), round(lon, 4), radius_km),
            scan_produce,
            settings=settings,
            ttl_s=settings.discovery_cache_ttl_s,
        )
    except disc.DiscoveryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    overlaps = await session.run_sync(
        lambda s: dq.annotate_overlaps(s, [(it["osm_id"], it["geometry"]) for it in items_raw])
    )
    items = [
        DiscoveredWaterBodyOut(**it, already_registered_id=overlaps.get(it["osm_id"]))
        for it in items_raw
    ]
    return SearchAndDiscoverResponse(
        query=body.query,
        resolved_place=geo["display_name"],
        centre=[lon, lat],
        district=geo["district"],
        state=geo["state"],
        radius_km=radius_km,
        items=items,
        total=len(items),
    )


@router.post(
    "/water-bodies/import-dynamic",
    response_model=ImportDynamicResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_dynamic(
    body: ImportDynamicRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: ActorDep,
) -> ImportDynamicResponse:
    """Register a water body found through ``search-and-discover``: upsert it
    into ``water_bodies`` and generate its Voronoi monitoring zones. Requires
    ``X-API-Key``.

    Re-importing the same water body (same id, derived or given) updates it in
    place and answers 200 rather than failing, matching how the bulk loader's
    upsert already behaves.
    """
    try:
        result = await session.run_sync(
            lambda s: dq.import_discovered(
                s,
                body.geometry,
                name=body.name,
                district=body.district,
                kind=body.kind,
                water_body_id=body.water_body_id,
                tier=body.tier,
                source=body.source,
            )
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    if not result["created"]:
        response.status_code = status.HTTP_200_OK
    return ImportDynamicResponse.model_validate(result)
