from datetime import UTC, date, datetime
from typing import Any, ClassVar

import httpx
import pytest
from pystac import Asset, Item
from shapely.geometry import box

from app.core.config import Settings
from app.services.l03_ingestion import stac as stac_mod
from app.services.l03_ingestion.stac import (
    BANDS,
    CDSESource,
    ChainedSource,
    EarthSearchSource,
    SceneCandidate,
    SourceError,
    build_source,
)

AOI = box(73.70, 18.38, 73.78, 18.45)


def _earth_item(
    item_id: str = "S2A_43QCA_20260714_0_L2A", cloud: float = 12.5, **extra: Any
) -> Item:
    props = {
        "datetime": "2026-07-14T05:44:12.620000Z",
        "eo:cloud_cover": cloud,
        "platform": "sentinel-2a",
        "grid:code": "MGRS-43QCA",
        "proj:code": "EPSG:32643",
        **extra,
    }
    item = Item(
        id=item_id,
        geometry={
            "type": "Polygon",
            "coordinates": [[[73, 18], [74, 18], [74, 19], [73, 19], [73, 18]]],
        },
        bbox=[73, 18, 74, 19],
        datetime=datetime(2026, 7, 14, 5, 44, 12, tzinfo=UTC),
        properties=props,
    )
    base = f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/{item_id}"
    for key, fname in (
        ("green", "B03"),
        ("red", "B04"),
        ("rededge1", "B05"),
        ("nir", "B08"),
        ("swir16", "B11"),
        ("scl", "SCL"),
    ):
        item.add_asset(key, Asset(href=f"{base}/{fname}.tif"))
    item.set_self_href(
        f"https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/{item_id}"
    )
    return item


class _FakeSearch:
    def __init__(self, items: list[Item]) -> None:
        self._items = items

    def items(self) -> list[Item]:
        return self._items


class _FakeClient:
    def __init__(self, items: list[Item], fail: bool = False) -> None:
        self._items = items
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> _FakeSearch:
        self.calls.append(kwargs)
        if self.fail:
            raise httpx.ConnectError("boom")
        return _FakeSearch(self._items)


def test_earth_search_candidate_mapping() -> None:
    src = EarthSearchSource("https://example/v1")
    fake = _FakeClient([_earth_item()])
    src._client = fake  # type: ignore[assignment]
    cands = src.search(AOI, date(2026, 7, 1), date(2026, 7, 31), max_cloud_pct=30, tiles=["43QCA"])
    assert len(cands) == 1
    c = cands[0]
    assert c.id == "S2A_43QCA_20260714_0_L2A"
    assert c.mgrs_tile == "43QCA" and c.epsg == 32643 and c.platform == "sentinel-2a"
    assert c.sensed_at == datetime(2026, 7, 14, 5, 44, 12, tzinfo=UTC)
    assert c.cloud_pct == 12.5 and c.source == "earth-search"
    assert set(c.assets) == set(BANDS)
    assert c.assets["B11"].endswith("/B11.tif")
    assert c.stac_href.endswith("/items/S2A_43QCA_20260714_0_L2A")
    call = fake.calls[0]
    assert call["collections"] == ["sentinel-2-l2a"]
    assert call["query"] == {"eo:cloud_cover": {"lte": 30}}
    assert call["datetime"] == "2026-07-01T00:00:00Z/2026-07-31T23:59:59Z"


def test_search_filters_to_requested_tiles_and_sorts() -> None:
    later = _earth_item("S2B_43QCA_20260719_0_L2A")
    other = _earth_item("S2B_43QDA_20260716_0_L2A")
    other.properties["grid:code"] = "MGRS-43QDA"
    src = EarthSearchSource("https://example/v1")
    src._client = _FakeClient([later, other, _earth_item()])  # type: ignore[assignment]
    ids = [c.id for c in src.search(AOI, date(2026, 7, 1), date(2026, 7, 31), tiles=["43QCA"])]
    assert ids == ["S2A_43QCA_20260714_0_L2A", "S2B_43QCA_20260719_0_L2A"]


def test_item_missing_a_band_is_dropped() -> None:
    item = _earth_item()
    del item.assets["swir16"]
    src = EarthSearchSource("https://example/v1")
    src._client = _FakeClient([item])  # type: ignore[assignment]
    assert src.search(AOI, date(2026, 7, 1), date(2026, 7, 31)) == []


def test_tile_from_mgrs_fields_when_grid_code_absent() -> None:
    item = _earth_item()
    del item.properties["grid:code"]
    item.properties.update(
        {"mgrs:utm_zone": 43, "mgrs:latitude_band": "Q", "mgrs:grid_square": "CA"}
    )
    assert stac_mod._tile_of(item) == "43QCA"


def test_search_failure_becomes_source_error() -> None:
    src = EarthSearchSource("https://example/v1")
    src._client = _FakeClient([], fail=True)  # type: ignore[assignment]
    with pytest.raises(SourceError, match="earth-search"):
        src.search(AOI, date(2026, 7, 1), date(2026, 7, 31))


# --- CDSE ---------------------------------------------------------------------


def test_cdse_token_refreshes_on_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1000.0}
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, data: dict[str, str], timeout: float) -> httpx.Response:
        calls.append(data)
        return httpx.Response(
            200,
            json={"access_token": f"tok{len(calls)}", "expires_in": 600},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    src = CDSESource(
        "https://cdse/v1/", "https://cdse/token", "id", "secret", clock=lambda: now["t"]
    )
    assert src.token() == "tok1"
    assert src.token() == "tok1"  # cached
    now["t"] += 550  # inside the 60 s refresh margin
    assert src.token() == "tok2"
    assert calls[0]["grant_type"] == "client_credentials"
    assert src.gdal_env() == {"GDAL_HTTP_HEADERS": "Authorization: Bearer tok2"}


def test_cdse_without_credentials_raises_source_error() -> None:
    src = CDSESource("https://cdse/v1/", "https://cdse/token", None, None)
    with pytest.raises(SourceError, match="CDSE_CLIENT_ID"):
        src.token()


def test_cdse_prefers_https_alternate_href() -> None:
    src = CDSESource("https://cdse/v1/", "https://cdse/token", "id", "secret")
    item = _earth_item()
    item.assets.clear()
    for key in ("B03_10m", "B04_10m", "B05_20m", "B08_10m", "B11_20m", "SCL_20m"):
        a = Asset(href=f"s3://eodata/S2/{key}.jp2")
        a.extra_fields["alternate"] = {
            "https": {"href": f"https://download.dataspace.copernicus.eu/{key}.jp2"}
        }
        item.add_asset(key, a)
    src.token = lambda: "tok"  # type: ignore[method-assign]
    cand = src._to_candidate(item)
    assert cand is not None
    assert cand.assets["B04"] == "https://download.dataspace.copernicus.eu/B04_10m.jp2"
    assert cand.gdal_env["GDAL_HTTP_HEADERS"].endswith("tok")


# --- fallback chain -----------------------------------------------------------


class _Stub:
    name: ClassVar[str] = "stub"

    def __init__(
        self, name: str, result: list[SceneCandidate] | None = None, fail: bool = False
    ) -> None:
        self.label, self.result, self.fail, self.calls = name, result or [], fail, 0

    def search(self, *a: Any, **k: Any) -> list[SceneCandidate]:
        self.calls += 1
        if self.fail:
            raise SourceError(f"{self.label} down")
        return self.result

    def gdal_env(self) -> dict[str, str]:
        return {}


def _cand(source: str) -> SceneCandidate:
    return SceneCandidate(
        id="x",
        mgrs_tile="43QCA",
        sensed_at=datetime.now(UTC),
        cloud_pct=1.0,
        platform=None,
        stac_href="h",
        source=source,
        assets=dict.fromkeys(BANDS, "u"),
    )


def test_chain_falls_back_and_records_source() -> None:
    primary, secondary = _Stub("a", fail=True), _Stub("b", [_cand("b")])
    chain = ChainedSource([primary, secondary])
    out = chain.search(AOI, date(2026, 1, 1), date(2026, 1, 2))
    assert [c.source for c in out] == ["b"]
    assert primary.calls == 1 and secondary.calls == 1


def test_chain_raises_when_all_fail() -> None:
    chain = ChainedSource([_Stub("a", fail=True), _Stub("b", fail=True)])
    with pytest.raises(SourceError, match="b down"):
        chain.search(AOI, date(2026, 1, 1), date(2026, 1, 2))


def test_build_source_respects_config() -> None:
    s = Settings(_env_file=None, stac_source="earth-search", stac_fallback=True)
    chain = build_source(s)
    assert isinstance(chain, ChainedSource)
    assert [x.name for x in chain.sources] == ["earth-search", "cdse"]
    s2 = Settings(_env_file=None, stac_source="cdse", stac_fallback=False)
    assert isinstance(build_source(s2), CDSESource)
