"""Wishlist and recent-history (S14).

The contract-shape checks run anywhere; the behaviour checks need Postgres and a
seeded registry, so they are integration-marked like the rest of l02_api.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.main import app
from app.services.l02_api import wishlist as q

WB = "wb_khadakwasla"
WB2 = "wb_ambazari_lake"
WB3 = "wb_bhama_askhed"


# --- contract (no database) ------------------------------------------------------


def test_openapi_exposes_the_five_endpoints() -> None:
    paths = app.openapi()["paths"]
    assert {"get", "post"} <= set(paths["/api/v1/wishlist"])
    assert "delete" in paths["/api/v1/wishlist/{item_id}"]
    assert "get" in paths["/api/v1/history/recent"]
    assert "post" in paths["/api/v1/history/recent/{water_body_id}"]


def test_saved_rows_carry_the_same_status_shape_as_the_water_body_list() -> None:
    """The sidebar must not invent a second notion of "how is this lake doing"."""
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["WishlistItemOut"]["properties"]["water_body"]["$ref"].endswith(
        "WaterBodyListItem"
    )
    assert schemas["RecentItem"]["properties"]["water_body"]["$ref"].endswith("WaterBodyListItem")


# --- behaviour (Postgres + seeded registry) --------------------------------------


@pytest.fixture
def clean() -> Any:
    """Both tables are UI state, so a test may empty them and put nothing back."""
    from app.db.sync_session import sync_session

    def _wipe(s: Any) -> None:
        s.execute(text("DELETE FROM wishlist_items"))
        s.execute(text("DELETE FROM recent_water_bodies"))
        s.commit()

    with sync_session() as s:
        _wipe(s)
        yield s
        _wipe(s)


@pytest.mark.integration
async def test_save_list_and_remove(client: AsyncClient, clean: Any) -> None:
    r = await client.post("/api/v1/wishlist", json={"water_body_id": WB, "custom_name": "My Dam"})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["custom_name"] == "My Dam"
    # hydrated with live status, so the sidebar needs no second request per row
    assert item["water_body"]["id"] == WB
    assert item["water_body"]["area_km2"] > 0
    assert "status" in item["water_body"]

    r = await client.get("/api/v1/wishlist")
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = await client.delete(f"/api/v1/wishlist/{item['id']}")
    assert r.status_code == 204
    assert (await client.get("/api/v1/wishlist")).json()["total"] == 0


@pytest.mark.integration
async def test_saving_the_same_water_body_twice_renames_it(client: AsyncClient, clean: Any) -> None:
    """The star button is idempotent: a repeat save must not 500 or duplicate."""
    first = await client.post("/api/v1/wishlist", json={"water_body_id": WB, "custom_name": "A"})
    assert first.status_code == 201
    again = await client.post("/api/v1/wishlist", json={"water_body_id": WB, "custom_name": "B"})
    assert again.status_code == 200  # updated, not created
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["custom_name"] == "B"
    assert (await client.get("/api/v1/wishlist")).json()["total"] == 1


@pytest.mark.integration
async def test_an_omitted_label_falls_back_to_the_water_bodys_own_name(
    client: AsyncClient, clean: Any
) -> None:
    r = await client.post("/api/v1/wishlist", json={"water_body_id": WB})
    assert r.status_code == 201
    assert r.json()["custom_name"] == r.json()["water_body"]["name"]


@pytest.mark.integration
async def test_unknown_ids_are_404_not_500(client: AsyncClient, clean: Any) -> None:
    saved = await client.post("/api/v1/wishlist", json={"water_body_id": "nope"})
    assert saved.status_code == 404
    assert (await client.delete("/api/v1/wishlist/999999")).status_code == 404
    assert (await client.post("/api/v1/history/recent/nope")).status_code == 404


@pytest.mark.integration
async def test_revisiting_moves_a_water_body_to_the_top(client: AsyncClient, clean: Any) -> None:
    for wb in (WB, WB2, WB3):
        assert (await client.post(f"/api/v1/history/recent/{wb}")).status_code == 204
    listed = (await client.get("/api/v1/history/recent")).json()["items"]
    assert [i["water_body_id"] for i in listed] == [WB3, WB2, WB]
    await client.post(f"/api/v1/history/recent/{WB}")
    items = (await client.get("/api/v1/history/recent")).json()["items"]
    assert items[0]["water_body_id"] == WB
    assert len(items) == 3, "revisiting must move the row, not add one"


@pytest.mark.integration
async def test_history_is_capped_so_the_table_cannot_grow(client: AsyncClient, clean: Any) -> None:
    """The cap is enforced in the table, not just in the response."""
    bodies = (await client.get("/api/v1/water-bodies?limit=50")).json()["items"]
    ids = [b["id"] for b in bodies][: q.RECENT_LIMIT + 4]
    assert len(ids) > q.RECENT_LIMIT, "seed registry too small to test the cap"
    for wb in ids:
        await client.post(f"/api/v1/history/recent/{wb}")
    listed = (await client.get("/api/v1/history/recent?limit=50")).json()
    assert listed["total"] == q.RECENT_LIMIT
    rows = clean.execute(text("SELECT count(*) FROM recent_water_bodies")).scalar_one()
    assert rows == q.RECENT_LIMIT


@pytest.mark.integration
async def test_recent_rows_say_whether_they_are_also_saved(client: AsyncClient, clean: Any) -> None:
    await client.post("/api/v1/wishlist", json={"water_body_id": WB})
    for wb in (WB, WB2):
        await client.post(f"/api/v1/history/recent/{wb}")
    flags = {
        i["water_body_id"]: i["wishlisted"]
        for i in (await client.get("/api/v1/history/recent")).json()["items"]
    }
    assert flags == {WB: True, WB2: False}


@pytest.mark.integration
async def test_deleting_a_water_body_takes_its_ui_state_with_it(
    client: AsyncClient, clean: Any
) -> None:
    """Both tables hang off water_bodies with ON DELETE CASCADE, so retiring a
    water body must not leave rows pointing at a row that is gone."""
    throwaway = "wb_ui_state_cascade_probe"
    clean.execute(
        text(
            "INSERT INTO water_bodies (id, name, district, kind, tier, geom, area_km2, mgrs_tiles)"
            " VALUES (:i, 'Cascade Probe', 'Test', 'lake', 3,"
            " ST_GeomFromText('MULTIPOLYGON(((73.7 18.4, 73.71 18.4, 73.71 18.41,"
            " 73.7 18.41, 73.7 18.4)))', 4326), 0.5, ARRAY['43QCA'])"
        ),
        {"i": throwaway},
    )
    clean.commit()
    try:
        assert (
            await client.post("/api/v1/wishlist", json={"water_body_id": throwaway})
        ).status_code == 201
        assert (await client.post(f"/api/v1/history/recent/{throwaway}")).status_code == 204

        clean.execute(text("DELETE FROM water_bodies WHERE id = :i"), {"i": throwaway})
        clean.commit()

        for table in ("wishlist_items", "recent_water_bodies"):
            left = clean.execute(
                text(f"SELECT count(*) FROM {table} WHERE water_body_id = :i"),
                {"i": throwaway},
            ).scalar_one()
            assert left == 0, f"{table} kept a row for a deleted water body"
    finally:
        clean.execute(text("DELETE FROM water_bodies WHERE id = :i"), {"i": throwaway})
        clean.commit()
