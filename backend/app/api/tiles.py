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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.ratelimit import limiter
from app.services.l05_water_detection.chips import chip_key

router = APIRouter(tags=["tiles"])


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


def titiler_params(chip: str, settings: Settings) -> dict[str, str]:
    style = LAYER_STYLES.get(layer_of(chip), LayerStyle(None, None))
    params: dict[str, str] = {"url": f"s3://{settings.minio_bucket}/{chip}"}
    if style.colormap_name:
        params["colormap_name"] = style.colormap_name
    if style.rescale:
        params["rescale"] = f"{style.rescale[0]},{style.rescale[1]}"
    if style.colormap:
        params["colormap"] = json.dumps(style.colormap)
    if style.nodata is not None:
        params["nodata"] = str(style.nodata)
    return params


async def _proxy(chip: str, z: int, x: int, y: int, settings: Settings) -> Response:
    url = f"{settings.titiler_url.rstrip('/')}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=titiler_params(chip, settings))
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


@router.get("/tiles/chip/{chip}/{z}/{x}/{y}.png", response_class=Response)
@limiter.limit(get_settings().rate_limit_tiles)
async def chip_tile(
    request: Request,
    chip: Annotated[str, Path(description="URL-encoded MinIO object key of a COG chip")],
    z: int,
    x: int,
    y: int,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
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
