"""Live satellite imagery straight from Google Earth Engine.

``GET /imagery/live`` returns a styled Earth Engine map id for a bbox: the most
recent Sentinel-2 pass (or a cloud-free composite) as true colour, false colour
or a water-masked index. Google renders the XYZ tiles, so the dashboard can show
"what does the lake look like right now" without waiting for ingestion. Map ids
are cached in Redis for ``gee_map_ttl_s`` per (bbox, visualisation, window).

Read-only, so no API key; the per-client rate limit applies. Every response is
imagery, not a finding: the product boundary (alerts carry ``disclaimer``) is
untouched because nothing here is scored.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.cache import cache_key, cached_json
from app.core.config import Settings, get_settings
from app.schemas.imagery import ImageryStatus, LiveImageryOut, LiveVisOut
from app.services.l03_ingestion import gee

log = logging.getLogger(__name__)

router = APIRouter(prefix="/imagery", tags=["imagery"])

BBox = tuple[float, float, float, float]


def parse_bbox(raw: str, max_edge_deg: float) -> BBox:
    try:
        parts = [float(p) for p in raw.split(",")]
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "bbox must be numeric") from exc
    if len(parts) != 4:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "bbox is minx,miny,maxx,maxy")
    minx, miny, maxx, maxy = parts
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "bbox out of range")
    if maxx - minx > max_edge_deg or maxy - miny > max_edge_deg:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"bbox edge wider than {max_edge_deg} degrees"
        )
    return minx, miny, maxx, maxy


def vis_catalogue() -> list[LiveVisOut]:
    return [
        LiveVisOut(
            key=v.key,
            label=v.label,
            kind="rgb" if v.kind == "rgb" else "index",
            water_only=v.water_only,
            description=v.description,
            palette=[f"#{c}" for c in v.palette],
            range=(v.vmin, v.vmax) if v.kind == "index" else None,
        )
        for v in gee.LIVE_VIS.values()
    ]


def _require_enabled(settings: Settings) -> None:
    if not settings.gee_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "live imagery is off: set GEE_ENABLED=true, GEE_PROJECT and a service account key",
        )


@router.get("/status", response_model=ImageryStatus)
async def imagery_status(settings: Annotated[Settings, Depends(get_settings)]) -> ImageryStatus:
    """Whether Earth Engine is switched on, configured and reachable, plus the
    visualisations ``/imagery/live`` accepts."""
    ok, error = False, None
    if settings.gee_enabled:
        try:
            await asyncio.to_thread(gee.ping, settings)
            ok = True
        except Exception as exc:
            error = str(exc)[:500]
    return ImageryStatus(
        enabled=settings.gee_enabled,
        configured=gee.configured(settings),
        ok=ok,
        project=settings.gee_project,
        collection=settings.gee_collection,
        error=error,
        visualisations=vis_catalogue(),
    )


@router.get("/live", response_model=LiveImageryOut)
async def live_imagery(
    settings: Annotated[Settings, Depends(get_settings)],
    bbox: Annotated[str, Query(description="minx,miny,maxx,maxy in WGS84 (lon/lat)")],
    vis: Annotated[str, Query(description="truecolor | falsecolor | ndti | ndci | mndwi")] = (
        "truecolor"
    ),
    date_to: Annotated[
        date | None, Query(alias="date", description="End of the window; default today")
    ] = None,
    days: Annotated[int, Query(ge=1, description="Lookback window in days")] = 30,
    max_cloud: Annotated[float, Query(ge=0, le=100)] = 60.0,
    composite: Annotated[
        bool, Query(description="Cloud-masked median of the window instead of the latest pass")
    ] = False,
) -> LiveImageryOut:
    """Styled XYZ tile template for the most recent Sentinel-2 image over ``bbox``.

    ``mode=latest`` mosaics every tile of the newest pass day (unmasked, so
    clouds are visible as clouds); ``composite=true`` returns the cloud-masked
    median of all passes in the window. Index layers are masked to water
    (MNDWI > 0) so the colour ramp only ever paints the lake."""
    _require_enabled(settings)
    box = parse_bbox(bbox, settings.gee_live_max_bbox_deg)
    if vis not in gee.LIVE_VIS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown visualisation {vis!r}; see /imagery/status"
        )
    if days > settings.gee_live_max_days:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"days must be <= {settings.gee_live_max_days}"
        )
    params: dict[str, Any] = {
        "vis": vis,
        "date_to": date_to.isoformat() if date_to else None,
        "days": days,
        "max_cloud_pct": max_cloud,
        "composite": composite,
    }

    async def produce() -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                gee.live_map,
                box,
                vis=vis,
                date_to=date_to,
                days=days,
                max_cloud_pct=max_cloud,
                composite=composite,
                settings=settings,
            )
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except gee.GEEError as exc:
            log.warning("live imagery failed", extra={"error": str(exc)})
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)[:500]) from exc
        return result.as_dict()

    # Map ids expire on Google's side after a few hours, so the cache TTL is its own.
    ttl_settings = settings.model_copy(update={"api_cache_ttl_s": settings.gee_map_ttl_s})
    key = cache_key(*gee.live_map_cache_parts(box, **params))
    value, hit = await cached_json(key, produce, settings=ttl_settings)
    return LiveImageryOut.model_validate({**value, "cached": hit})
