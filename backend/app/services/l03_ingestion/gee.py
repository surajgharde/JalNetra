"""Google Earth Engine as a third Sentinel-2 source, plus live map layers.

Two jobs live here:

* ``GEESource`` -- the ``STACSource`` protocol over ``COPERNICUS/S2_SR_HARMONIZED``.
  Search is one ``getInfo`` over the filtered collection; a scene's "assets" are
  ``gee://<asset id>#<band>`` hrefs, which ``reader.read_windowed_bands`` routes to
  :func:`read_windowed_bands_gee`. Pixels come back through ``computePixels`` on
  the *same* snapped 10 m UTM grid the COG reader uses, in blocks that stay under
  Earth Engine's 48 MB response cap, so L4-L6 never see a different array shape.
* :func:`live_map` -- a styled Earth Engine map id (true colour, false colour or a
  water-masked index) for the latest pass or a cloud-free composite over a bbox.
  Tiles are rendered by Google on demand; nothing is ingested or stored.

Earth Engine has no API keys. It authenticates a Google Cloud service account
whose JSON key is configured via ``GEE_SERVICE_ACCOUNT_KEY`` (path) or
``GEE_SERVICE_ACCOUNT_KEY_JSON`` (inline); with neither it falls back to the
application-default credentials that ``earthengine authenticate`` writes. The
harmonized collection already has ESA's BOA add-offset removed on every scene,
so ``boa_add_offset`` is always 0 here.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import numpy as np
import requests
from affine import Affine
from pyproj import CRS
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from tenacity import (
    Retrying,
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_exponential_jitter,
)

from app.core.config import Settings, get_settings
from app.services.l03_ingestion.reader import (
    BUFFER_M,
    GRID_M,
    WindowedBands,
    _aoi_bounds_in,
    snapped_window_bounds,
)
from app.services.l03_ingestion.stac import BANDS, SceneCandidate, SourceError

log = logging.getLogger(__name__)

SCHEME = "gee://"
SOURCE_NAME = "gee"

# Canonical pipeline band -> Earth Engine band name in S2_SR_HARMONIZED.
GEE_BANDS: Mapping[str, str] = {
    "B03": "B3",
    "B04": "B4",
    "B05": "B5",
    "B08": "B8",
    "B11": "B11",
    "SCL": "SCL",
}
BILINEAR_BANDS: frozenset[str] = frozenset({"B05", "B11"})  # 20 m reflectance bands
COMPUTE_PIXELS_MAX_BYTES = 48 * 1024 * 1024
# SCL classes treated as not-ground for composites and index layers:
# 3 cloud shadow, 8/9 cloud medium/high, 10 cirrus, 11 snow. 0 (no data) is masked too.
SCL_MASKED: tuple[int, ...] = (0, 3, 8, 9, 10, 11)
ATTRIBUTION = "Contains modified Copernicus Sentinel data, processed in Google Earth Engine"


class GEEError(SourceError):
    """Earth Engine call failed (auth, quota, network, bad asset)."""


# --- session ---------------------------------------------------------------------

_lock = threading.Lock()
_initialised_for: str | None = None

# Substrings that mark a failure as "the link dropped", not "the request was wrong".
# Earth Engine's own errors arrive as EEException with the transport error stringified
# inside them, so matching on text is the only way to tell the two apart.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "ssl",
    "eof occurred",
    "connection",
    "connectionreset",
    "broken pipe",
    "timed out",
    "timeout",
    "max retries",
    "remote end closed",
    "temporarily unavailable",
    "transport",
    "502",
    "503",
    "504",
)


def _transient(exc: BaseException) -> bool:
    """True when ``exc`` looks like a dropped connection rather than a rejected request.

    ``requests``/``urllib3``/``ssl``/``socket`` errors all subclass :class:`OSError`, which
    covers the common case; the text match catches wrapped ones (``EEException``,
    ``google.auth`` transport errors) whose type says nothing useful.
    """
    if isinstance(exc, FileNotFoundError | PermissionError | IsADirectoryError):
        return False  # OSError subclasses, but a bad key path is not a flaky link
    if isinstance(exc, OSError):
        return True
    return any(m in f"{type(exc).__name__}: {exc}".lower() for m in _TRANSIENT_MARKERS)


def _harden_session(settings: Settings) -> None:
    """Give Earth Engine's ``requests`` session connect/read retries with backoff.

    ``ee`` builds a bare :class:`requests.Session`, and ``requests`` ships with retries
    off, so a single TLS EOF anywhere -- the OAuth token POST at startup, the hourly
    token refresh, any ``getInfo``/``computePixels`` -- surfaces as a hard failure.
    Mounting a retrying adapter makes those survive a flaky link. ``allowed_methods=None``
    is deliberate: the token exchange is a POST, and retrying a dropped connection is
    safe because the request never reached the server.
    """
    import ee
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    n = max(0, settings.gee_transport_retries)
    if not n:
        return
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=n,
            connect=n,
            read=n,
            status=n,
            backoff_factor=1.0,
            status_forcelist=(408, 429, 500, 502, 503, 504),
            allowed_methods=None,
            raise_on_status=False,
        )
    )
    try:
        state = ee.data._get_state()  # private: ee exposes no hook for its session
        if state.requests_session is None:
            state.requests_session = requests.Session()
        state.requests_session.mount("https://", adapter)
        state.requests_session.mount("http://", adapter)
    except Exception:  # pragma: no cover -- ee internals moved; retries below still apply
        log.warning("could not attach retries to the earth engine session", exc_info=True)


def _session_key(settings: Settings) -> str:
    return "|".join(
        str(p)
        for p in (
            settings.gee_project,
            settings.gee_service_account,
            settings.gee_service_account_key,
            bool(settings.gee_service_account_key_json),
            settings.gee_high_volume,
        )
    )


def credentials(settings: Settings) -> Any | None:
    """Service-account credentials from the configured key, or ``None`` for ADC."""
    import ee

    if settings.gee_service_account_key_json:
        return ee.ServiceAccountCredentials(
            settings.gee_service_account, key_data=settings.gee_service_account_key_json
        )
    if settings.gee_service_account_key:
        path = settings.gee_service_account_key  # relative paths resolve from the backend cwd
        if not path.exists():
            raise GEEError(f"gee: service account key {path} not found")
        return ee.ServiceAccountCredentials(settings.gee_service_account, key_file=str(path))
    return None


def _ee_initialize(creds: Any | None, project: str | None, url: str | None) -> None:
    """The one Earth Engine call that reaches the network during startup."""
    import ee

    if creds is None:
        ee.Initialize(project=project, url=url)
    else:
        ee.Initialize(creds, project=project, url=url)


def _deadline(ms: int) -> None:
    import ee

    ee.data.setDeadline(ms)


def _high_volume_url() -> str:
    import ee

    return str(ee.data.HIGH_VOLUME_API_BASE_URL)


def initialize(settings: Settings | None = None, *, force: bool = False) -> None:
    """Initialise the Earth Engine session once per process (thread-safe, idempotent)."""
    global _initialised_for
    settings = settings or get_settings()
    if not settings.gee_enabled:
        raise GEEError("gee: GEE_ENABLED=false (set GEE_PROJECT and a service account key)")
    key = _session_key(settings)
    with _lock:
        if _initialised_for == key and not force:
            return
        creds = credentials(settings)
        url = _high_volume_url() if settings.gee_high_volume else None
        _harden_session(settings)

        # The OAuth token exchange behind ee.Initialize is one POST to
        # oauth2.googleapis.com, and on a lossy link it is dropped mid-handshake
        # ("[SSL: UNEXPECTED_EOF_WHILE_READING]") often enough to fail startup outright.
        # The adapter above retries inside a single call; this retries the call itself,
        # which also covers the paths google-auth drives through a session of its own.
        try:
            for attempt in Retrying(
                retry=retry_if_exception(_transient),
                stop=stop_after_attempt(max(1, settings.gee_init_attempts)),
                wait=wait_exponential_jitter(initial=1, max=30),
                reraise=True,
            ):
                with attempt:
                    n = attempt.retry_state.attempt_number
                    if n > 1:
                        log.warning(
                            "earth engine initialise retry",
                            extra={"attempt": n, "attempts": settings.gee_init_attempts},
                        )
                    _ee_initialize(creds, settings.gee_project, url)
        except Exception as exc:
            hint = (
                " (the link to oauth2.googleapis.com keeps dropping -- check the network,"
                " VPN or proxy; GEE_INIT_ATTEMPTS raises the number of tries)"
                if _transient(exc)
                else ""
            )
            raise GEEError(f"gee: initialise failed: {exc}{hint}") from exc
        _deadline(int(settings.gee_timeout_s * 1000))
        _harden_session(settings)  # ee.Initialize may have swapped the session
        _initialised_for = key
        log.info(
            "earth engine session ready",
            extra={
                "project": settings.gee_project,
                "service_account": bool(creds),
                "high_volume": settings.gee_high_volume,
            },
        )


def configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.gee_enabled and settings.gee_project)


def _ee_ping() -> None:
    """The one trivial round trip /health makes to Earth Engine."""
    import ee

    ee.Number(1).getInfo()


def ping(settings: Settings | None = None) -> None:
    """One trivial server round trip; raises :class:`GEEError` when the session is unusable.

    Retried like ``initialize``: this probe decides what /health reports, and on a
    lossy link a single dropped connection was enough to mark Earth Engine -- and so
    the whole API -- degraded while the session was in fact perfectly usable.
    """
    settings = settings or get_settings()
    initialize(settings)

    try:
        for attempt in Retrying(
            retry=retry_if_exception(_transient),
            stop=stop_after_attempt(max(1, settings.gee_ping_attempts)),
            wait=wait_exponential_jitter(initial=0.5, max=2),
            reraise=True,
        ):
            with attempt:
                _ee_ping()
    except Exception as exc:
        raise GEEError(f"gee: ping failed: {exc}") from exc


# --- pure helpers (unit-tested without a session) ------------------------------------


def asset_href(asset_id: str, band: str) -> str:
    return f"{SCHEME}{asset_id}#{GEE_BANDS[band]}"


def is_gee_href(href: str) -> bool:
    return href.startswith(SCHEME)


def split_href(href: str) -> tuple[str, str]:
    """``gee://COPERNICUS/S2_SR_HARMONIZED/<index>#B4`` -> (asset id, band)."""
    if not is_gee_href(href):
        raise ValueError(f"not a gee:// href: {href}")
    asset, _, band = href.removeprefix(SCHEME).partition("#")
    return asset, band


def epsg_of_tile(tile: str) -> int:
    """UTM EPSG for an MGRS tile id such as ``43QCA`` (northern bands N..X, south C..M)."""
    zone = int(tile[:2])
    return (32600 if tile[2].upper() >= "N" else 32700) + zone


def candidate_from_feature(feature: Mapping[str, Any], collection: str) -> SceneCandidate | None:
    """One ``ImageCollection.getInfo()`` feature -> provider-agnostic candidate."""
    props = feature.get("properties") or {}
    index = props.get("system:index") or str(feature.get("id", "")).rsplit("/", 1)[-1]
    tile = props.get("MGRS_TILE")
    start = props.get("system:time_start")
    if not index or not tile or start is None:
        return None
    asset_id = f"{collection}/{index}"
    epsg: int | None = None
    for band in feature.get("bands") or []:
        crs = band.get("crs")
        if isinstance(crs, str) and crs.startswith("EPSG:"):
            epsg = int(crs.split(":")[1])
            break
    if epsg is None:
        epsg = epsg_of_tile(str(tile))
    cloud = props.get("CLOUDY_PIXEL_PERCENTAGE")
    platform = props.get("SPACECRAFT_NAME")
    return SceneCandidate(
        id=str(index),
        mgrs_tile=str(tile),
        sensed_at=datetime.fromtimestamp(float(start) / 1000.0, tz=UTC),
        cloud_pct=float(cloud) if cloud is not None else 100.0,
        platform=str(platform).lower().replace(" ", "-") if platform else None,
        stac_href=f"{SCHEME}{asset_id}",
        source=SOURCE_NAME,
        assets={b: asset_href(asset_id, b) for b in BANDS},
        epsg=epsg,
        boa_add_offset=0,  # harmonized collection: offset removed on every scene
    )


def plan_blocks(width: int, height: int, block_px: int) -> Iterator[tuple[int, int, int, int]]:
    """(col_off, row_off, w, h) blocks covering a width x height grid."""
    if block_px <= 0:
        raise ValueError("block_px must be positive")
    for row in range(0, height, block_px):
        for col in range(0, width, block_px):
            yield col, row, min(block_px, width - col), min(block_px, height - row)


def block_px_for(n_bands: int, bytes_per_px: int = 2, cap: int = COMPUTE_PIXELS_MAX_BYTES) -> int:
    """Largest square block edge whose ``n_bands`` response stays under Earth Engine's cap
    (with a 25 % safety margin for encoding overhead)."""
    return int(((cap * 0.75) / (n_bands * bytes_per_px)) ** 0.5)


def grid_params(
    crs: str, transform: Affine, col_off: int, row_off: int, width: int, height: int
) -> dict[str, Any]:
    """``computePixels`` ``grid`` for a block of the shared 10 m grid."""
    x0, y0 = transform * (col_off, row_off)
    return {
        "dimensions": {"width": width, "height": height},
        "affineTransform": {
            "scaleX": transform.a,
            "shearX": transform.b,
            "translateX": x0,
            "shearY": transform.d,
            "scaleY": transform.e,
            "translateY": y0,
        },
        "crsCode": crs,
    }


def raster_bounds_of(band_info: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) of a band from its ``crs_transform`` + ``dimensions``."""
    a, b, c, d, e, f = band_info["crs_transform"]
    w, h = band_info["dimensions"]
    t = Affine(a, b, c, d, e, f)
    xs, ys = zip(*(t * p for p in ((0, 0), (w, 0), (0, h), (w, h))), strict=True)
    return float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))


# --- source ---------------------------------------------------------------------


@dataclass
class GEESource:
    """``STACSource`` over an Earth Engine Sentinel-2 L2A collection."""

    collection: str
    settings: Settings
    name: ClassVar[str] = SOURCE_NAME

    def _collection(self, geom: BaseGeometry, date_from: date, date_to: date) -> Any:
        import ee

        return (
            ee.ImageCollection(self.collection)
            .filterBounds(ee.Geometry(mapping(geom)))
            .filterDate(date_from.isoformat(), (date_to + timedelta(days=1)).isoformat())
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
        initialize(self.settings)
        import ee

        try:
            coll = self._collection(geom, date_from, date_to)
            if max_cloud_pct is not None:
                coll = coll.filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
            if tiles:
                coll = coll.filter(ee.Filter.inList("MGRS_TILE", list(tiles)))
            # B4 only: the feature list then carries one band's crs per scene and stays small.
            info = coll.select(["B4"]).limit(limit).getInfo()
        except GEEError:
            raise
        except Exception as exc:
            raise GEEError(f"gee: search failed: {exc}") from exc

        features = (info or {}).get("features") or []
        out = [c for f in features if (c := candidate_from_feature(f, self.collection))]
        out.sort(key=lambda c: (c.sensed_at, c.id))
        log.info(
            "gee search", extra={"source": self.name, "items": len(features), "kept": len(out)}
        )
        return out

    def gdal_env(self) -> dict[str, str]:
        return {}


def build_gee_source(settings: Settings | None = None) -> GEESource:
    settings = settings or get_settings()
    return GEESource(collection=settings.gee_collection, settings=settings)


# --- windowed reads via computePixels ---------------------------------------------


_retry = retry(
    retry=retry_if_exception_type((OSError, ConnectionError, GEEError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)


@_retry
def _compute_block(expression: Any, grid: dict[str, Any]) -> np.ndarray:
    import ee

    try:
        arr: np.ndarray = ee.data.computePixels(
            {"expression": expression, "fileFormat": "NUMPY_NDARRAY", "grid": grid}
        )
    except ee.ee_exception.EEException as exc:
        # Quota / transient server errors are retried; a bad asset is not.
        msg = str(exc)
        if any(t in msg for t in ("Too many", "quota", "429", "500", "503", "deadline")):
            raise GEEError(f"gee: computePixels transient failure: {msg}") from exc
        raise
    return arr


def read_windowed_bands_gee(
    candidate: SceneCandidate,
    aoi_4326: BaseGeometry,
    water_body_id: str,
    *,
    buffer_m: float = BUFFER_M,
    bands: tuple[str, ...] = BANDS,
    settings: Settings | None = None,
) -> WindowedBands:
    """Same contract and grid conventions as ``reader.read_windowed_bands``."""
    settings = settings or get_settings()
    initialize(settings)
    import ee

    started = time.perf_counter()
    asset_id, _ = split_href(candidate.assets[bands[0]])
    image = ee.Image(asset_id)

    # Reference grid from B4 (10 m): crs, origin and tile extent in one round trip.
    try:
        info = image.select(["B4"]).getInfo()
    except Exception as exc:
        raise GEEError(f"gee: cannot describe {asset_id}: {exc}") from exc
    band_info = (info or {}).get("bands", [{}])[0]
    crs = CRS.from_user_input(band_info["crs"])
    a, b, c, d, e, f = band_info["crs_transform"]
    ref_transform = Affine(a, b, c, d, e, f)
    if abs(ref_transform.a - GRID_M) > 1e-6:
        raise ValueError(f"reference band B4 is {ref_transform.a} m, expected {GRID_M} m")
    aoi_bounds = _aoi_bounds_in(crs, aoi_4326, buffer_m)
    bounds = snapped_window_bounds(aoi_bounds, ref_transform, raster_bounds_of(band_info))

    width = round((bounds[2] - bounds[0]) / GRID_M)
    height = round((bounds[3] - bounds[1]) / GRID_M)
    transform = Affine(GRID_M, 0.0, bounds[0], 0.0, -GRID_M, bounds[3])
    crs_code = f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_wkt()

    # 20 m reflectance bands are resampled bilinearly onto the 10 m grid, everything
    # else nearest (SCL is categorical) -- exactly what the COG reader does.
    nearest = [GEE_BANDS[b] for b in bands if b not in BILINEAR_BANDS]
    bilinear = [GEE_BANDS[b] for b in bands if b in BILINEAR_BANDS]
    expression = image.select(nearest)
    if bilinear:
        expression = expression.addBands(image.select(bilinear).resample("bilinear"))
    expression = expression.select([GEE_BANDS[b] for b in bands]).toUint16()

    arrays: dict[str, np.ndarray] = {
        b: np.zeros((height, width), dtype=np.uint8 if b == "SCL" else np.uint16) for b in bands
    }
    block = min(settings.gee_block_px, block_px_for(len(bands)))
    n_blocks = 0
    for col, row, w, h in plan_blocks(width, height, block):
        data = _compute_block(expression, grid_params(crs_code, transform, col, row, w, h))
        for b in bands:
            arrays[b][row : row + h, col : col + w] = data[GEE_BANDS[b]].astype(
                arrays[b].dtype, copy=False
            )
        n_blocks += 1

    result = WindowedBands(
        scene_id=candidate.id,
        water_body_id=water_body_id,
        crs=crs_code,
        transform=transform,
        bounds=bounds,
        arrays=arrays,
        bytes_read=int(sum(x.nbytes for x in arrays.values())),
        duration_s=round(time.perf_counter() - started, 3),
    )
    log.info(
        "gee windowed read complete",
        extra={
            "scene_id": candidate.id,
            "water_body_id": water_body_id,
            "shape": [height, width],
            "blocks": n_blocks,
            "bytes_read": result.bytes_read,
            "duration_s": result.duration_s,
            "source": candidate.source,
        },
    )
    return result


# --- live imagery (map ids) ------------------------------------------------------------


@dataclass(frozen=True)
class LiveVis:
    key: str
    label: str
    kind: str  # "rgb" | "index"
    bands: tuple[str, ...]  # RGB bands, or (numerator, denominator) for an index
    vmin: float
    vmax: float
    palette: tuple[str, ...] = ()
    gamma: float | None = None
    water_only: bool = False
    description: str = ""


LIVE_VIS: Mapping[str, LiveVis] = {
    "truecolor": LiveVis(
        "truecolor",
        "True colour",
        "rgb",
        ("B4", "B3", "B2"),
        0,
        3000,
        gamma=1.3,
        description="Sentinel-2 surface reflectance, red/green/blue",
    ),
    "falsecolor": LiveVis(
        "falsecolor",
        "False colour (NIR)",
        "rgb",
        ("B8", "B4", "B3"),
        0,
        4000,
        gamma=1.2,
        description="NIR/red/green: vegetation red, water dark",
    ),
    "ndti": LiveVis(
        "ndti",
        "Turbidity (NDTI)",
        "index",
        ("B4", "B3"),
        -0.3,
        0.5,
        palette=("fff7bc", "fec44f", "fe9929", "d95f0e", "993404"),
        water_only=True,
        description="(red - green) / (red + green) over water pixels",
    ),
    "ndci": LiveVis(
        "ndci",
        "Chlorophyll-a (NDCI)",
        "index",
        ("B5", "B4"),
        -0.2,
        0.4,
        palette=("f7fcf5", "c7e9c0", "74c476", "238b45", "00441b"),
        water_only=True,
        description="(rededge - red) / (rededge + red) over water pixels",
    ),
    "mndwi": LiveVis(
        "mndwi",
        "Water extent (MNDWI)",
        "index",
        ("B3", "B11"),
        -0.5,
        0.8,
        palette=("f7fbff", "c6dbef", "6baed6", "2171b5", "08306b"),
        description="(green - SWIR) / (green + SWIR); water is positive",
    ),
}


def vis_params(vis: LiveVis) -> dict[str, Any]:
    """``ee.Image.visualize`` keyword arguments for a live layer."""
    params: dict[str, Any] = {"min": vis.vmin, "max": vis.vmax}
    if vis.kind == "rgb":
        params["bands"] = list(vis.bands)
        if vis.gamma is not None:
            params["gamma"] = vis.gamma
    else:
        params["palette"] = list(vis.palette)
    return params


def _mask_clouds(img: Any) -> Any:
    import ee

    scl = img.select("SCL")
    mask = ee.Image.constant(1)
    for cls in SCL_MASKED:
        mask = mask.And(scl.neq(cls))
    return img.updateMask(mask)


def _styled(img: Any, vis: LiveVis) -> Any:
    if vis.kind == "rgb":
        return img.visualize(**vis_params(vis))
    num, den = vis.bands
    index = img.normalizedDifference([num, den]).rename(vis.key)
    if vis.water_only:
        water = img.normalizedDifference(["B3", "B11"]).gt(0)
        index = index.updateMask(water)
    return index.visualize(**vis_params(vis))


@dataclass(frozen=True)
class LiveMap:
    vis: str
    mode: str  # "latest" | "composite"
    tile_url: str
    map_id: str
    scene_date: date  # latest pass in the window (composite: last contributing pass)
    scene_count: int  # passes contributing to the rendered image
    cloud_pct: float | None  # mean tile-level cloud cover of the contributing passes
    window_from: date
    window_to: date
    collection: str
    attribution: str = ATTRIBUTION

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["scene_date"] = self.scene_date.isoformat()
        d["window_from"] = self.window_from.isoformat()
        d["window_to"] = self.window_to.isoformat()
        return d


def live_map(
    bbox: tuple[float, float, float, float],
    *,
    vis: str = "truecolor",
    date_to: date | None = None,
    days: int = 30,
    max_cloud_pct: float = 60.0,
    composite: bool = False,
    settings: Settings | None = None,
) -> LiveMap:
    """Styled Earth Engine map for ``bbox`` (lon/lat).

    ``latest``: mosaic of the most recent pass day in the window (all tiles that
    day), unmasked so the user sees the real image. ``composite``: cloud-masked
    median of every pass in the window, which is what a "cloud-free" view is.
    """
    settings = settings or get_settings()
    if vis not in LIVE_VIS:
        raise ValueError(f"unknown live visualisation {vis!r}")
    initialize(settings)
    import ee

    end = date_to or datetime.now(UTC).date()
    start = end - timedelta(days=days)
    region = ee.Geometry.Rectangle(list(bbox), "EPSG:4326", False)
    try:
        coll = (
            ee.ImageCollection(settings.gee_collection)
            .filterBounds(region)
            .filterDate(start.isoformat(), (end + timedelta(days=1)).isoformat())
            .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
        )
        latest_ms = coll.aggregate_max("system:time_start").getInfo()
        if latest_ms is None:
            raise LookupError(f"no Sentinel-2 pass under {max_cloud_pct}% cloud in the window")
        latest_day = datetime.fromtimestamp(float(latest_ms) / 1000.0, tz=UTC).date()
        if composite:
            used = coll
            image = coll.map(_mask_clouds).median()
        else:
            used = coll.filterDate(
                latest_day.isoformat(), (latest_day + timedelta(days=1)).isoformat()
            )
            image = used.mosaic()
        meta: dict[str, Any] = (
            ee.Dictionary(
                {"count": used.size(), "cloud": used.aggregate_mean("CLOUDY_PIXEL_PERCENTAGE")}
            ).getInfo()
            or {}
        )
        mapid = _styled(image.clip(region), LIVE_VIS[vis]).getMapId({})
    except (GEEError, LookupError):
        raise
    except Exception as exc:
        raise GEEError(f"gee: live map failed: {exc}") from exc

    return LiveMap(
        vis=vis,
        mode="composite" if composite else "latest",
        tile_url=str(mapid["tile_fetcher"].url_format),
        map_id=str(mapid["mapid"]),
        scene_date=latest_day,
        scene_count=int(meta["count"]),
        cloud_pct=round(float(meta["cloud"]), 1) if meta.get("cloud") is not None else None,
        window_from=start,
        window_to=end,
        collection=settings.gee_collection,
    )


def truecolor_thumbnail(
    bbox: tuple[float, float, float, float],
    day: date,
    *,
    px: int = 640,
    settings: Settings | None = None,
    timeout_s: float | None = None,
) -> bytes | None:
    """PNG of the true-colour Sentinel-2 mosaic over ``bbox`` on ``day`` (all tiles
    of that pass, unmasked), or ``None`` when no pass exists. For reports.

    ``timeout_s`` caps the PNG fetch; callers rendering many days in one request
    pass something well under ``gee_timeout_s``, which is sized for pixel reads.
    """
    settings = settings or get_settings()
    initialize(settings)
    import ee
    import httpx

    region = ee.Geometry.Rectangle(list(bbox), "EPSG:4326", False)
    coll = (
        ee.ImageCollection(settings.gee_collection)
        .filterBounds(region)
        .filterDate(day.isoformat(), (day + timedelta(days=1)).isoformat())
    )
    try:
        if not coll.size().getInfo():
            return None
        styled = _styled(coll.mosaic(), LIVE_VIS["truecolor"])
        url = styled.getThumbURL({"region": region, "dimensions": px, "format": "png"})
        r = httpx.get(url, timeout=timeout_s or settings.gee_timeout_s, follow_redirects=True)
        r.raise_for_status()
        return bytes(r.content)
    except Exception as exc:
        raise GEEError(f"gee: thumbnail failed for {day}: {exc}") from exc


def live_map_cache_parts(bbox: tuple[float, float, float, float], **kw: Any) -> tuple[Any, ...]:
    """Stable cache-key parts: bbox rounded to ~100 m so map pans do not thrash the cache."""
    rounded = tuple(round(v, 3) for v in bbox)
    return ("gee-live", json.dumps(rounded), *(f"{k}={v}" for k, v in sorted(kw.items())))
