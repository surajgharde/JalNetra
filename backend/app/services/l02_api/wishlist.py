"""Saved water bodies and recently inspected ones (S14).

Thin layer over two tables. The interesting part is that neither list invents a
notion of "how is this lake doing": both hydrate each row through
:func:`app.services.l02_api.water_bodies.body_item`, the same helper that backs
``GET /water-bodies``, so the sidebar and the main list can never disagree.

Synchronous, like the rest of l02_api: the API drives them with
``AsyncSession.run_sync``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import RecentWaterBody, WaterBody, WishlistItem
from app.services.l02_api.water_bodies import body_item

log = logging.getLogger(__name__)

RECENT_LIMIT = 10  # the sidebar shows the last 10 inspected water bodies


def _water_body(session: Session, water_body_id: str) -> WaterBody:
    wb = session.get(WaterBody, water_body_id)
    if wb is None:
        raise LookupError(water_body_id)
    return wb


def _item_out(session: Session, item: WishlistItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "water_body_id": item.water_body_id,
        "custom_name": item.custom_name,
        "notes": item.notes,
        "created_at": item.created_at,
        "water_body": body_item(session, _water_body(session, item.water_body_id)),
    }


def list_wishlist(session: Session) -> dict[str, Any]:
    """Every saved water body, newest first, each with its live status."""
    items = list(
        session.execute(select(WishlistItem).order_by(WishlistItem.created_at.desc()))
        .scalars()
        .all()
    )
    out = [_item_out(session, i) for i in items]
    return {"items": out, "total": len(out)}


def add_to_wishlist(
    session: Session,
    water_body_id: str,
    *,
    custom_name: str | None = None,
    notes: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Save a water body. Returns (item, created).

    Saving one that is already saved updates its label instead of failing: the
    table holds one row per water body, so a repeat save is a rename.
    """
    wb = _water_body(session, water_body_id)  # 404 before we write anything
    label = (custom_name or "").strip() or wb.name
    existing = session.execute(
        select(WishlistItem).where(WishlistItem.water_body_id == water_body_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.custom_name = label
        if notes is not None:
            existing.notes = notes
        session.flush()
        log.info("wishlist updated", extra={"water_body_id": water_body_id})
        return _item_out(session, existing), False

    item = WishlistItem(water_body_id=water_body_id, custom_name=label, notes=notes)
    session.add(item)
    session.flush()
    log.info("wishlist added", extra={"water_body_id": water_body_id})
    return _item_out(session, item), True


def remove_from_wishlist(session: Session, item_id: int) -> None:
    item = session.get(WishlistItem, item_id)
    if item is None:
        raise LookupError(str(item_id))
    session.delete(item)
    session.flush()
    log.info("wishlist removed", extra={"water_body_id": item.water_body_id})


def list_recent(session: Session, limit: int = RECENT_LIMIT) -> dict[str, Any]:
    """The last ``limit`` water bodies inspected, most recent first."""
    rows = list(
        session.execute(
            select(RecentWaterBody)
            .order_by(RecentWaterBody.last_viewed_at.desc())
            .limit(max(1, limit))
        )
        .scalars()
        .all()
    )
    saved = (
        set(
            session.execute(
                select(WishlistItem.water_body_id).where(
                    WishlistItem.water_body_id.in_([r.water_body_id for r in rows])
                )
            )
            .scalars()
            .all()
        )
        if rows
        else set()
    )
    items = [
        {
            "water_body_id": r.water_body_id,
            "last_viewed_at": r.last_viewed_at,
            "wishlisted": r.water_body_id in saved,
            "water_body": body_item(session, _water_body(session, r.water_body_id)),
        }
        for r in rows
    ]
    return {"items": items, "total": len(items)}


def touch_recent(session: Session, water_body_id: str) -> dict[str, Any]:
    """Record that a water body was just inspected, and trim the list.

    One row per water body, so revisiting moves it to the top rather than
    growing the table. Anything past ``RECENT_LIMIT`` is dropped here so the
    table stays the size of what the sidebar shows.
    """
    _water_body(session, water_body_id)  # 404 before we write anything
    row = session.execute(
        select(RecentWaterBody).where(RecentWaterBody.water_body_id == water_body_id)
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = RecentWaterBody(water_body_id=water_body_id, last_viewed_at=now)
        session.add(row)
    else:
        row.last_viewed_at = now
    session.flush()

    keep = (
        select(RecentWaterBody.id)
        .order_by(RecentWaterBody.last_viewed_at.desc())
        .limit(RECENT_LIMIT)
        .scalar_subquery()
    )
    session.execute(delete(RecentWaterBody).where(RecentWaterBody.id.not_in(keep)))
    session.flush()

    total = session.execute(select(func.count()).select_from(RecentWaterBody)).scalar_one()
    return {
        "water_body_id": water_body_id,
        "last_viewed_at": row.last_viewed_at,
        "tracked": int(total),
    }
