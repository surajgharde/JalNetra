"""STAC scene discovery with two interchangeable sources and automatic fallback.

* ``EarthSearchSource`` -- AWS Earth Search v1, no auth, Sentinel-2 L2A COGs. Default.
* ``CDSESource`` -- Copernicus Data Space Ecosystem STAC. Search is public; asset
  reads need an OAuth2 client-credentials token, refreshed on expiry and passed
  to GDAL as a bearer header.
* ``GEESource`` (``gee.py``) -- Google Earth Engine, opt-in via ``GEE_ENABLED``.
* ``ChainedSource`` -- tries sources in order; the source that served a scene is
  recorded on the ``scenes`` row so an outage never silently changes provenance.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, ClassVar, Protocol

import httpx
from pystac import Item
from pystac_client import Client
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.core import metrics
from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

# Canonical band keys used throughout the pipeline.
BANDS: tuple[str, ...] = ("B03", "B04", "B05", "B08", "B11", "SCL")
BAND_RESOLUTION_M: Mapping[str, int] = {
    "B03": 10,
    "B04": 10,
    "B05": 20,
    "B08": 10,
    "B11": 20,
    "SCL": 20,
}


@dataclass(frozen=True)
class SceneCandidate:
    """Provider-agnostic description of one Sentinel-2 pass over one tile."""

    id: str
    mgrs_tile: str
    sensed_at: datetime
    cloud_pct: float
    platform: str | None
    stac_href: str
    source: str
    assets: dict[str, str]  # canonical band -> href readable by GDAL
    epsg: int | None = None
    boa_add_offset: int = 0  # DN units to add before /10000; see Scene.boa_add_offset
    gdal_env: dict[str, str] = field(default_factory=dict)  # extra GDAL config for reads


class SourceError(RuntimeError):
    """A STAC source failed (network, auth, 5xx). Triggers fallback."""


class STACSource(Protocol):
    name: ClassVar[str]

    def search(
        self,
        geom: BaseGeometry,
        date_from: date,
        date_to: date,
        *,
        max_cloud_pct: float | None = None,
        tiles: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[SceneCandidate]: ...

    def gdal_env(self) -> dict[str, str]: ...


def _tile_of(item: Item) -> str | None:
    p = item.properties
    code = p.get("grid:code")
    if isinstance(code, str) and code.startswith("MGRS-"):
        return code.removeprefix("MGRS-")
    zone, band, square = (
        p.get("mgrs:utm_zone"),
        p.get("mgrs:latitude_band"),
        p.get("mgrs:grid_square"),
    )
    if zone and band and square:
        return f"{int(zone):02d}{band}{square}"
    tile = p.get("s2:mgrs_tile") or p.get("tileId")
    return str(tile).removeprefix("T") if tile else None


def _epsg_of(item: Item) -> int | None:
    p = item.properties
    if isinstance(p.get("proj:epsg"), int):
        return int(p["proj:epsg"])
    code = p.get("proj:code")
    if isinstance(code, str) and code.startswith("EPSG:"):
        return int(code.split(":")[1])
    return None


BOA_ADD_OFFSET_RAW = -1000  # DN units ESA adds since processing baseline 04.00


def _boa_add_offset_of(item: Item) -> int:
    """DN units to add before scaling to reflectance (see ``Scene.boa_add_offset``).
    Earth Search rewrites its COGs with the offset already removed and says so;
    raw ESA data (CDSE) carries it from baseline 04.00 onwards."""
    p = item.properties
    if p.get("earthsearch:boa_offset_applied") is True:
        return 0
    baseline = p.get("s2:processing_baseline") or p.get("processing:baseline")
    try:
        return BOA_ADD_OFFSET_RAW if float(str(baseline)) >= 4.0 else 0
    except (TypeError, ValueError):
        # Unknown baseline: every scene since Jan 2022 and the reprocessed archive carry it.
        return BOA_ADD_OFFSET_RAW


class _BaseStacSource:
    name: ClassVar[str] = "stac"
    asset_keys: ClassVar[Mapping[str, str]] = {}

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        self.url = url
        self.timeout = timeout
        self._client: Client | None = None

    def _open(self) -> Client:
        if self._client is None:
            try:
                self._client = Client.open(self.url)
            except Exception as exc:  # pystac-client raises assorted httpx/APIError types
                raise SourceError(f"{self.name}: cannot open catalog {self.url}: {exc}") from exc
        return self._client

    def _asset_href(self, item: Item, band: str) -> str | None:
        key = self.asset_keys.get(band)
        asset = item.assets.get(key) if key else None
        return asset.href if asset else None

    def _to_candidate(self, item: Item) -> SceneCandidate | None:
        tile = _tile_of(item)
        if not tile or item.datetime is None:
            return None
        assets = {b: h for b in BANDS if (h := self._asset_href(item, b))}
        if set(assets) != set(BANDS):
            log.warning("scene missing bands", extra={"scene_id": item.id, "have": sorted(assets)})
            return None
        cloud = item.properties.get("eo:cloud_cover")
        return SceneCandidate(
            id=item.id,
            mgrs_tile=tile,
            sensed_at=item.datetime.astimezone(UTC),
            cloud_pct=float(cloud) if cloud is not None else 100.0,
            platform=item.properties.get("platform"),
            stac_href=item.get_self_href() or f"{self.url.rstrip('/')}/items/{item.id}",
            source=self.name,
            assets=assets,
            epsg=_epsg_of(item),
            boa_add_offset=_boa_add_offset_of(item),
            gdal_env=self.gdal_env(),
        )

    def search(
        self,
        geom: BaseGeometry,
        date_from: date,
        date_to: date,
        *,
        max_cloud_pct: float | None = None,
        tiles: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[SceneCandidate]:
        query: dict[str, Any] | None = (
            {"eo:cloud_cover": {"lte": max_cloud_pct}} if max_cloud_pct is not None else None
        )
        try:
            search = self._open().search(
                collections=["sentinel-2-l2a"],
                intersects=mapping(geom),
                datetime=f"{date_from.isoformat()}T00:00:00Z/{date_to.isoformat()}T23:59:59Z",
                query=query,
                max_items=limit,
            )
            items = list(search.items())
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"{self.name}: search failed: {exc}") from exc

        wanted = set(tiles) if tiles else None
        out: list[SceneCandidate] = []
        for item in items:
            cand = self._to_candidate(item)
            if cand is None or (wanted and cand.mgrs_tile not in wanted):
                continue
            out.append(cand)
        out.sort(key=lambda c: (c.sensed_at, c.id))
        log.info("stac search", extra={"source": self.name, "items": len(items), "kept": len(out)})
        return out

    def gdal_env(self) -> dict[str, str]:
        return {}


class EarthSearchSource(_BaseStacSource):
    name: ClassVar[str] = "earth-search"
    asset_keys: ClassVar[Mapping[str, str]] = {
        "B03": "green",
        "B04": "red",
        "B05": "rededge1",
        "B08": "nir",
        "B11": "swir16",
        "SCL": "scl",
    }


class CDSESource(_BaseStacSource):
    """CDSE STAC. Asset hrefs are ``s3://eodata/...``; when an HTTPS alternate is
    published we prefer it and authenticate reads with a bearer token."""

    name: ClassVar[str] = "cdse"
    asset_keys: ClassVar[Mapping[str, str]] = {
        "B03": "B03_10m",
        "B04": "B04_10m",
        "B05": "B05_20m",
        "B08": "B08_10m",
        "B11": "B11_20m",
        "SCL": "SCL_20m",
    }

    def __init__(
        self,
        url: str,
        token_url: str,
        client_id: str | None,
        client_secret: str | None,
        timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(url, timeout)
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._clock = clock
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- auth -----------------------------------------------------------------

    def token(self) -> str:
        """Client-credentials access token, refreshed 60 s before expiry."""
        if self._token and self._clock() < self._token_expiry - 60:
            return self._token
        if not self.client_id or not self.client_secret:
            raise SourceError("cdse: CDSE_CLIENT_ID / CDSE_CLIENT_SECRET not configured")
        try:
            r = httpx.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            payload = r.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"cdse: token request failed: {exc}") from exc
        self._token = str(payload["access_token"])
        self._token_expiry = self._clock() + float(payload.get("expires_in", 600))
        return self._token

    def gdal_env(self) -> dict[str, str]:
        return {"GDAL_HTTP_HEADERS": f"Authorization: Bearer {self.token()}"}

    def _asset_href(self, item: Item, band: str) -> str | None:
        key = self.asset_keys.get(band)
        asset = item.assets.get(key) if key else None
        if asset is None:
            return None
        alt = asset.extra_fields.get("alternate", {})
        https = alt.get("https", {}).get("href") if isinstance(alt, dict) else None
        return str(https) if https else asset.href


class ChainedSource:
    """Try each source in order; raise the last error only if all fail."""

    name: ClassVar[str] = "chain"

    def __init__(self, sources: Sequence[STACSource]) -> None:
        if not sources:
            raise ValueError("ChainedSource needs at least one source")
        self.sources = list(sources)

    def search(
        self,
        geom: BaseGeometry,
        date_from: date,
        date_to: date,
        *,
        max_cloud_pct: float | None = None,
        tiles: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[SceneCandidate]:
        last: Exception | None = None
        for i, src in enumerate(self.sources):
            try:
                found = src.search(
                    geom, date_from, date_to, max_cloud_pct=max_cloud_pct, tiles=tiles, limit=limit
                )
            except SourceError as exc:
                last = exc
                metrics.stac_request_failures.labels(source=src.name).inc()
                log.warning(
                    "stac source failed, falling back",
                    extra={"source": src.name, "error": str(exc)},
                )
                continue
            if i > 0:
                metrics.stac_fallbacks.labels(served_by=src.name).inc()
                log.info("stac search served by fallback", extra={"source": src.name})
            return found
        assert last is not None
        raise last

    def gdal_env(self) -> dict[str, str]:
        return self.sources[0].gdal_env()


def build_source(settings: Settings | None = None) -> STACSource:
    """The configured primary source, chained with the others as fallbacks.
    Google Earth Engine joins the chain only when ``GEE_ENABLED`` is set."""
    settings = settings or get_settings()
    sources: dict[str, STACSource] = {
        "earth-search": EarthSearchSource(settings.earth_search_url),
        "cdse": CDSESource(
            settings.cdse_stac_url,
            settings.cdse_token_url,
            settings.cdse_client_id,
            settings.cdse_client_secret,
        ),
    }
    if settings.gee_enabled:
        from app.services.l03_ingestion.gee import build_gee_source

        sources["gee"] = build_gee_source(settings)
    if settings.stac_source not in sources:
        raise ValueError(f"STAC_SOURCE={settings.stac_source!r} needs GEE_ENABLED=true")
    primary = sources.pop(settings.stac_source)
    if not settings.stac_fallback:
        return primary
    return ChainedSource([primary, *sources.values()])
