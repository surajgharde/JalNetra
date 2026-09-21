"""Water body, observation, indicator and series responses (S9).
Field names follow the frozen API contract; examples feed /docs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.l10_explain.explain import DISCLAIMER

BodyStatus = Literal["normal", "watch", "alert", "baseline_building", "no_data"]


class LatestObservation(BaseModel):
    scene_id: str
    observed_on: date
    cloud_pct: float
    usable: bool
    stage: str = Field(
        description="deepest stage reached: ingested|masked|indicators|anomalies|scored|alerted"
    )


class WaterBodyListItem(BaseModel):
    id: str
    name: str
    district: str
    kind: str
    tier: int
    area_km2: float
    centroid: list[float] = Field(description="[lon, lat]")
    bbox: list[float] = Field(description="[minlon, minlat, maxlon, maxlat]")
    n_zones: int
    latest_observation: LatestObservation | None
    status: BodyStatus
    open_alerts: int
    max_open_severity: Literal["low", "medium", "high"] | None
    baseline_status: Literal["usable", "building", "none"]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "wb_khadakwasla",
                    "name": "Khadakwasla Reservoir",
                    "district": "Pune",
                    "kind": "reservoir",
                    "tier": 1,
                    "area_km2": 18.4,
                    "centroid": [73.7712, 18.4419],
                    "bbox": [73.72, 18.40, 73.82, 18.47],
                    "n_zones": 6,
                    "latest_observation": {
                        "scene_id": "S2C_43QCA_20260917_0_L2A",
                        "observed_on": "2026-09-17",
                        "cloud_pct": 8.1,
                        "usable": True,
                        "stage": "alerted",
                    },
                    "status": "alert",
                    "open_alerts": 1,
                    "max_open_severity": "high",
                    "baseline_status": "usable",
                }
            ]
        }
    )


class WaterBodyList(BaseModel):
    items: list[WaterBodyListItem]
    total: int
    next_cursor: str | None


class ZoneFeatureProps(BaseModel):
    id: str
    name: str
    seq: int
    area_km2: float
    baseline_status: Literal["usable", "building", "none"]
    open_alert_id: str | None


class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: dict[str, Any]
    properties: dict[str, Any]


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]


class WaterBodyDetail(WaterBodyListItem):
    mgrs_tiles: list[str]
    source: str | None
    boundary: dict[str, Any] = Field(description="GeoJSON MultiPolygon, EPSG:4326")
    zones: FeatureCollection
    disclaimer: str


class ObservationItem(BaseModel):
    scene_id: str
    observed_on: date
    sensed_at: datetime
    platform: str | None
    cloud_pct: float = Field(description="tile-level cloud cover from STAC")
    usable: bool = Field(description="mask verdict for this body (falls back to the tile flag)")
    valid_pixel_pct: float | None = Field(description="cloud-free share over the body")
    water_extent_km2: float | None
    stage: str
    source: str

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "scene_id": "S2C_43QCA_20260917_0_L2A",
                    "observed_on": "2026-09-17",
                    "sensed_at": "2026-09-17T05:31:12Z",
                    "platform": "sentinel-2c",
                    "cloud_pct": 8.1,
                    "usable": True,
                    "valid_pixel_pct": 91.9,
                    "water_extent_km2": 17.2,
                    "stage": "alerted",
                    "source": "earth-search",
                }
            ]
        }
    )


class ObservationList(BaseModel):
    water_body_id: str
    items: list[ObservationItem]
    total: int
    next_cursor: str | None


class ZoneIndicator(BaseModel):
    key: str
    display_name: str
    value: float | None
    p90: float | None
    baseline_mean: float | None
    baseline_std: float | None
    baseline_p10: float | None
    baseline_p90: float | None
    baseline_status: str
    z_score: float | None
    deviation_pct: float | None
    valid_pixel_pct: float | None
    water_fraction_pct: float | None
    scientific_basis: str


class ZoneIndicators(BaseModel):
    zone_id: str
    zone_name: str
    indicators: list[ZoneIndicator]
    rejected: list[dict[str, Any]] = Field(
        default_factory=list, description="zone-indicator records L6 refused, with reason"
    )


class IndicatorsResponse(BaseModel):
    water_body_id: str
    requested_date: date
    scene_id: str | None
    observed_on: date | None
    zones: list[ZoneIndicators]
    cached: bool
    disclaimer: str

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "water_body_id": "wb_khadakwasla",
                    "requested_date": "2026-09-17",
                    "scene_id": "S2C_43QCA_20260917_0_L2A",
                    "observed_on": "2026-09-17",
                    "zones": [
                        {
                            "zone_id": "wb_khadakwasla_z3",
                            "zone_name": "Eastern zone",
                            "indicators": [
                                {
                                    "key": "ndti_turbidity",
                                    "display_name": "Turbidity (NDTI)",
                                    "value": 0.312,
                                    "p90": 0.41,
                                    "baseline_mean": 0.128,
                                    "baseline_std": 0.021,
                                    "baseline_p10": 0.1,
                                    "baseline_p90": 0.16,
                                    "baseline_status": "usable",
                                    "z_score": 8.76,
                                    "deviation_pct": 143.8,
                                    "valid_pixel_pct": 91.9,
                                    "water_fraction_pct": 97.0,
                                    "scientific_basis": "Red-to-green ratio; rises with sediment.",
                                }
                            ],
                            "rejected": [],
                        }
                    ],
                    "cached": False,
                    "disclaimer": DISCLAIMER,
                }
            ]
        }
    )


class SeriesPoint(BaseModel):
    observed_at: datetime
    scene_id: str
    value: float | None = Field(description="zone mean")
    p90: float | None
    valid_pixel_pct: float | None
    baseline_mean: float | None
    baseline_p10: float | None
    baseline_p90: float | None
    baseline_status: str | None
    z_score: float | None


class SeriesResponse(BaseModel):
    water_body_id: str
    zone_id: str
    indicator: str
    display_name: str
    date_from: date
    date_to: date
    points: list[SeriesPoint]
    baseline_status: str
    baseline_usable_windows: int
    weekly: list[dict[str, Any]] = Field(
        default_factory=list, description="continuous-aggregate weekly means"
    )
    cached: bool
    disclaimer: str

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "water_body_id": "wb_khadakwasla",
                    "zone_id": "wb_khadakwasla_z3",
                    "indicator": "ndti_turbidity",
                    "display_name": "Turbidity (NDTI)",
                    "date_from": "2026-06-01",
                    "date_to": "2026-09-17",
                    "points": [
                        {
                            "observed_at": "2026-09-17T05:31:12Z",
                            "scene_id": "S2C_43QCA_20260917_0_L2A",
                            "value": 0.312,
                            "p90": 0.41,
                            "valid_pixel_pct": 91.9,
                            "baseline_mean": 0.128,
                            "baseline_p10": 0.1,
                            "baseline_p90": 0.16,
                            "baseline_status": "usable",
                            "z_score": 8.76,
                        }
                    ],
                    "baseline_status": "usable",
                    "baseline_usable_windows": 366,
                    "weekly": [],
                    "cached": False,
                    "disclaimer": DISCLAIMER,
                }
            ]
        }
    )
