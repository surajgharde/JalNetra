"""Live satellite imagery (Google Earth Engine) response models."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class LiveVisOut(BaseModel):
    key: str
    label: str
    kind: Literal["rgb", "index"]
    water_only: bool
    description: str
    palette: list[str] = Field(default_factory=list)  # hex colours, low -> high (index layers)
    range: tuple[float, float] | None = None  # index value range mapped onto the palette


class ImageryStatus(BaseModel):
    enabled: bool
    configured: bool  # a project id is set
    ok: bool  # a session could be opened and answered a request
    project: str | None
    collection: str
    error: str | None = None
    visualisations: list[LiveVisOut]


class LiveImageryOut(BaseModel):
    vis: str
    mode: Literal["latest", "composite"]
    tile_url: str = Field(description="XYZ template with {z}/{x}/{y}, served by Google")
    map_id: str
    scene_date: date = Field(description="Latest Sentinel-2 pass rendered")
    scene_count: int = Field(ge=0, description="Passes contributing to the image")
    cloud_pct: float | None = Field(
        default=None, description="Mean tile-level cloud cover of those passes"
    )
    window_from: date
    window_to: date
    collection: str
    attribution: str
    cached: bool = False

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "vis": "truecolor",
                    "mode": "latest",
                    "tile_url": "https://earthengine.googleapis.com/v1/projects/jalnetra/maps/abc/tiles/{z}/{x}/{y}",
                    "map_id": "projects/jalnetra/maps/abc",
                    "scene_date": "2026-09-19",
                    "scene_count": 2,
                    "cloud_pct": 8.4,
                    "window_from": "2026-08-22",
                    "window_to": "2026-09-21",
                    "collection": "COPERNICUS/S2_SR_HARMONIZED",
                    "attribution": (
                        "Contains modified Copernicus Sentinel data, "
                        "processed in Google Earth Engine"
                    ),
                    "cached": False,
                }
            ]
        }
    }
