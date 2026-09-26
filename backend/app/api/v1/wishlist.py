"""Wishlist and recent-history endpoints (S14).

UI state over the registry: pin a water body under a label that means something
locally, and keep the last few inspected ones to hand. Every row comes back
hydrated with the same live status ``GET /water-bodies`` returns, so the sidebar
never shows a different verdict from the main list.

Writes require ``X-API-Key`` like every other write endpoint.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ActorDep
from app.db.session import get_session
from app.schemas.wishlist import (
    RecentList,
    WishlistItemIn,
    WishlistItemOut,
    WishlistList,
)
from app.services.l02_api import wishlist as q

router = APIRouter(tags=["wishlist"])


@router.get("/wishlist", response_model=WishlistList)
async def list_wishlist(session: Annotated[AsyncSession, Depends(get_session)]) -> WishlistList:
    """Saved water bodies, newest first, each with its latest water-quality status."""
    data = await session.run_sync(q.list_wishlist)
    return WishlistList(**data)


@router.post("/wishlist", response_model=WishlistItemOut, status_code=status.HTTP_201_CREATED)
async def add_to_wishlist(
    body: WishlistItemIn,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: ActorDep,
) -> WishlistItemOut:
    """Save a water body under a custom label. Requires ``X-API-Key``.

    Saving one that is already saved renames it and answers 200 rather than
    failing, so the star button is idempotent.
    """
    try:
        item, created = await session.run_sync(
            lambda s: q.add_to_wishlist(
                s, body.water_body_id, custom_name=body.custom_name, notes=body.notes
            )
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"water body {exc} not found") from exc
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return WishlistItemOut(**item)


@router.delete("/wishlist/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_wishlist(
    item_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: ActorDep,
) -> Response:
    """Drop a water body from the wishlist. Requires ``X-API-Key``."""
    try:
        await session.run_sync(lambda s: q.remove_from_wishlist(s, item_id))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"wishlist item {exc} not found") from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/history/recent", response_model=RecentList)
async def list_recent(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=50)] = q.RECENT_LIMIT,
) -> RecentList:
    """The water bodies most recently inspected, most recent first."""
    data = await session.run_sync(lambda s: q.list_recent(s, limit=limit))
    return RecentList(**data)


@router.post("/history/recent/{water_body_id}", status_code=status.HTTP_204_NO_CONTENT)
async def touch_recent(
    water_body_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Record that a water body was just inspected.

    Unauthenticated on purpose: this is the UI reporting navigation, it writes
    no user content, and the table is capped at the handful of rows the sidebar
    shows, so it cannot be grown into a problem.
    """
    try:
        await session.run_sync(lambda s: q.touch_recent(s, water_body_id))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"water body {exc} not found") from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
