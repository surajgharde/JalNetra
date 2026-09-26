"""Saved water bodies and recently inspected ones (S14).

Both responses embed the same ``WaterBodyListItem`` the water-bodies list
returns, so a sidebar entry carries the live status, open-alert count and area
without a second round trip per row, and one definition of "status" serves the
whole UI.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.water_bodies import WaterBodyListItem


class WishlistItemIn(BaseModel):
    water_body_id: str = Field(description="id of a registered water body")
    custom_name: str | None = Field(
        default=None,
        max_length=200,
        description="label to show in the sidebar; defaults to the water body's own name",
    )
    notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "water_body_id": "wb_ambazari_lake",
                    "custom_name": "Nagpur Drinking Water Zone A",
                    "notes": "Upstream of the intake; check after every monsoon spell.",
                }
            ]
        }
    )


class WishlistItemOut(BaseModel):
    id: int
    water_body_id: str
    custom_name: str
    notes: str | None
    created_at: datetime
    water_body: WaterBodyListItem = Field(
        description="live status of the saved water body, same shape as /water-bodies"
    )


class WishlistList(BaseModel):
    items: list[WishlistItemOut]
    total: int


class RecentItem(BaseModel):
    water_body_id: str
    last_viewed_at: datetime
    wishlisted: bool = Field(description="true when this water body is also on the wishlist")
    water_body: WaterBodyListItem


class RecentList(BaseModel):
    items: list[RecentItem]
    total: int
