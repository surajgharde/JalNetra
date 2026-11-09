"""Raster tiles (S9): a thin proxy in front of TiTiler that fixes the style per
layer so the frontend never chooses colormaps.

Two URL forms, both resolving to one COG chip in MinIO:

* ``/tiles/chip/{chip_key}/{z}/{x}/{y}.png`` - any chip by its (URL-encoded)
  object key; this is what alert ``evidence`` links carry.
* ``/tiles/{layer}/{water_body_id}/{date}/{z}/{x}/{y}.png`` - the contract's
  ``/tiles/{layer}/...`` form for the body-scope chips L5/L6 write at
  ``chips/{water_body_id}/{date}/body/{layer}.tif``.

Styles: turbidity sequential amber, chlorophyll green, floating algae
yellow-green, sediment orange, water extent blue, water mask a single blue,
anomaly sequential red. Chips are immutable, so tiles get a long Cache-Control.

Each layer's colour ramp is stretched over *the chip's own* p2-p98 rather than
a fixed interval. The fixed intervals covered every value an index could
physically take, which is far wider than any one lake on any one day: at
Ambazari the sediment chip spans 0.038-0.056 inside a 0.0-0.3 ramp, so 94% of
the ramp went unused and every water pixel came out the same pale tan. The
layer painted the whole water body one flat colour and showed no structure at
all. The fixed interval is still the fallback, and still the answer when the
spread is small enough to be sensor noise -- see ``chip_rescale``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from app.core.cache import cache_key, cached_json
from app.core.config import Settings, get_settings
from app.core.ratelimit import limiter
from app.services.l05_water_detection.chips import chip_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["tiles"])

#: A p2-p98 spread narrower than this many sensor-noise units is treated as no
#: real variation, and the layer keeps its fixed ramp rather than being
#: stretched into a gradient made of noise.
NOISE_SPREAD_MULTIPLE = 2.0


@dataclass(frozen=True)
class LayerStyle:
    colormap_name: str | None
    rescale: tuple[float, float] | None
    colormap: dict[str, list[int]] | None = None  # explicit value -> RGBA
    nodata: float | int | None = None


LAYER_STYLES: dict[str, LayerStyle] = {
    "ndti_turbidity": LayerStyle("ylorbr", (-0.3, 0.5)),
    "ndci_chlorophyll": LayerStyle("greens", (-0.2, 0.4)),
    "fai_algal": LayerStyle("ylgn", (-0.05, 0.1)),
    "sediment_proxy": LayerStyle("oranges", (0.0, 0.3)),
    "mndwi_extent": LayerStyle("blues", (-0.5, 0.8)),
    "watermask": LayerStyle(None, None, colormap={"1": [31, 119, 180, 255]}, nodata=255),
    "anomaly": LayerStyle("reds", (0.0, 1.0)),
    "truecolor": LayerStyle(None, None),
}
ALIASES = {
    "turbidity": "ndti_turbidity",
    "chlorophyll": "ndci_chlorophyll",
    "fai": "fai_algal",
    "sediment": "sediment_proxy",
    "extent": "mndwi_extent",
    "mask": "watermask",
}


def layer_of(chip: str) -> str:
    name = chip.rsplit("/", 1)[-1].removesuffix(".tif")
    return ALIASES.get(name, name)


async def chip_statistics(chip: str, settings: Settings) -> dict[str, float] | None:
    """One chip's band statistics from TiTiler, cached.

    Chips are immutable, so this is cached for ``tile_stats_cache_ttl_s`` (a
    week by default) and costs one extra request per chip per week, not per
    tile. Returns None on any failure; the caller then keeps the fixed ramp.
    """

    async def _fetch() -> dict[str, float] | None:
        url = f"{settings.titiler_url.rstrip('/')}/cog/statistics"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    url, params={"url": f"s3://{settings.minio_bucket}/{chip}"}
                )
            if r.status_code != 200:
                return None
            bands = r.json()
            if not isinstance(bands, dict) or not bands:
                return None
            band = next(iter(bands.values()))
            return {
                k: float(band[k])
                for k in ("min", "max", "percentile_2", "percentile_98")
                if isinstance(band.get(k), (int, float))
            }
        except (httpx.HTTPError, ValueError, KeyError, StopIteration) as exc:
            log.warning("chip statistics failed", extra={"chip": chip, "error": str(exc)})
            return None

    value, _hit = await cached_json(
        cache_key("tilestats", chip),
        _fetch,
        settings=settings,
        ttl_s=settings.tile_stats_cache_ttl_s,
    )
    return value if isinstance(value, dict) and value else None


def stretch_from_stats(
    stats: dict[str, float], style: LayerStyle, *, noise_floor: float | None
) -> tuple[float, float] | None:
    """The interval to stretch this layer's ramp over, or None to keep the fixed one.

    p2-p98 rather than min-max so one hot pixel or one shadow does not flatten
    everything else back out again.

    The guard matters more than the stretch: percentile-stretching a lake that
    really is uniform turns sensor noise into a convincing gradient and invents
    structure that is not there. ``noise_floor`` is the per-indicator sensor
    noise the anomaly detector already uses (``anomaly_sigma_floor``); a spread
    narrower than a couple of those is noise, and the layer should render
    honestly flat on the fixed ramp instead.
    """
    lo = stats.get("percentile_2")
    hi = stats.get("percentile_98")
    if lo is None or hi is None or not (hi > lo):
        return None
    if noise_floor is not None and (hi - lo) < NOISE_SPREAD_MULTIPLE * noise_floor:
        return None
    # A little headroom so the extremes are not clipped to the ramp's end stops.
    pad = (hi - lo) * 0.05
    out = (lo - pad, hi + pad)
    if style.rescale:
        # Never widen past what the index can physically be: the fixed interval
        # is the indicator's real range, and going outside it would colour
        # values no sensor can produce.
        f_lo, f_hi = style.rescale
        out = (max(out[0], f_lo), min(out[1], f_hi))
        if not out[1] > out[0]:
            return None
    return out


def titiler_params(
    chip: str, settings: Settings, *, rescale: tuple[float, float] | None = None
) -> dict[str, str]:
    style = LAYER_STYLES.get(layer_of(chip), LayerStyle(None, None))
    params: dict[str, str] = {"url": f"s3://{settings.minio_bucket}/{chip}"}
    if style.colormap_name:
        params["colormap_name"] = style.colormap_name
    span = rescale or style.rescale
    if span:
        params["rescale"] = f"{span[0]},{span[1]}"
    if style.colormap:
        params["colormap"] = json.dumps(style.colormap)
    if style.nodata is not None:
        params["nodata"] = str(style.nodata)
    return params


async def adaptive_rescale(chip: str, settings: Settings) -> tuple[float, float] | None:
    """The per-chip stretch for this layer, or None to keep the fixed ramp."""
    if not settings.tile_adaptive_rescale:
        return None
    layer = layer_of(chip)
    style = LAYER_STYLES.get(layer)
    # A categorical layer (the water mask) has an explicit colormap and no ramp
    # to stretch; true colour is RGB and has no rescale either.
    if style is None or style.rescale is None or style.colormap is not None:
        return None
    stats = await chip_statistics(chip, settings)
    if stats is None:
        return None
    return stretch_from_stats(
        stats, style, noise_floor=settings.anomaly_sigma_floor.get(layer)
    )


async def _proxy(chip: str, z: int, x: int, y: int, settings: Settings) -> Response:
    url = f"{settings.titiler_url.rstrip('/')}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    rescale = await adaptive_rescale(chip, settings)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=titiler_params(chip, settings, rescale=rescale))
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"tile server unreachable: {exc}") from exc
    if r.status_code == 404 or (r.status_code == 500 and b"not found" in r.content.lower()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"chip {chip!r} not found")
    if r.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"tile server returned {r.status_code}")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/png"),
        headers={"Cache-Control": f"public, max-age={settings.tile_cache_max_age_s}"},
    )


# {chip:path}, not {chip}: a chip key always contains slashes ("chips/<wb>/<date>/..."),
# and the server percent-decodes %2F before routing, so a plain path parameter -- which
# never spans a slash -- matched no real key at all. Every alert's evidence tile URL
# is built from this route, so they all 404'd.
@router.get("/tiles/chip/{chip:path}/{z}/{x}/{y}.png", response_class=Response)
@limiter.limit(get_settings().rate_limit_tiles)
async def chip_tile(
    request: Request,
    chip: Annotated[str, Path(description="MinIO object key of a COG chip (may be URL-encoded)")],
    z: int,
    x: int,
    y: int,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    chip = chip.strip("/")
    if not chip.startswith(("chips/", "composites/")) or ".." in chip:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not a chip key")
    return await _proxy(chip, z, x, y, settings)


@router.get("/tiles/{layer}/{water_body_id}/{on}/{z}/{x}/{y}.png", response_class=Response)
@limiter.limit(get_settings().rate_limit_tiles)
async def layer_tile(
    request: Request,
    layer: str,
    water_body_id: str,
    on: date,
    z: int,
    x: int,
    y: int,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Body-scope chip of ``layer`` (``watermask``, ``ndti_turbidity``, ... or an alias
    such as ``turbidity``) for a water body on a date."""
    key = layer_of(layer)
    if key not in LAYER_STYLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown layer {layer!r}")
    return await _proxy(
        chip_key(water_body_id, on, key, prefix=settings.chip_prefix), z, x, y, settings
    )


@router.get("/tiles/styles")
async def tile_styles() -> dict[str, dict[str, object]]:
    """The style applied per layer, for the legend."""
    return {
        k: {
            "colormap_name": v.colormap_name,
            "rescale": v.rescale,
            "colormap": v.colormap,
            "nodata": v.nodata,
        }
        for k, v in LAYER_STYLES.items()
    }
