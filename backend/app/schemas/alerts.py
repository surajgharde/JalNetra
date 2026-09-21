"""Frozen alert contract (plan: "Internal API contract"). The mock JSON the
frontend is built against and these models must match field-for-field; add
fields, never rename them.

Conventions: dates ``YYYY-MM-DD``, timestamps RFC 3339 UTC, coordinates
``[lon, lat]`` EPSG:4326, areas km2, rainfall mm, indicator values unitless.
``disclaimer`` is carried verbatim on every alert; the frontend renders it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["low", "medium", "high"]
AlertStatus = Literal["open", "investigating", "validated", "dismissed"]


class WaterBodyRef(BaseModel):
    id: str
    name: str
    district: str


class ZoneRef(BaseModel):
    id: str
    name: str
    centroid: list[float] = Field(description="[lon, lat]", min_length=2, max_length=2)


class IndicatorReading(BaseModel):
    key: str
    value: float | None
    baseline_mean: float | None
    baseline_std: float | None
    z_score: float | None
    deviation_pct: float | None
    baseline_status: str | None = None


class ContributionOut(BaseModel):
    factor: str
    value: float
    key: str | None = None
    raw: float | None = None
    parts: dict[str, float] = Field(default_factory=dict)


class Explanation(BaseModel):
    summary: str
    contributions: list[ContributionOut] = Field(min_length=4, max_length=4)


class AlertContext(BaseModel):
    rainfall_72h_mm: float | None
    cloud_cover_pct: float | None
    natural_cause_likely: bool
    rainfall_percentile: float | None = None
    gate_reason: str | None = None
    baseline_status: str | None = None
    votes: int | None = None


class Evidence(BaseModel):
    baseline_composite_url: str | None
    current_observation_url: str | None
    anomaly_mask_url: str | None
    current_scene_id: str | None = None
    reference_scene_id: str | None = None
    reference_observed_on: date | None = None
    brief_url: str | None = None


class TimelineEntry(BaseModel):
    observed_at: datetime
    scene_id: str
    candidate_id: int | None
    priority_score: float | None
    severity: Severity | None
    confidence: float | None
    alertable: bool


class AlertOut(BaseModel):
    """GET /api/v1/alerts/{id} - the full alert."""

    model_config = ConfigDict(from_attributes=True)

    alert_id: str
    water_body: WaterBodyRef
    zone: ZoneRef
    observed_on: date
    affected_area_km2: float | None
    primary_indicator: str
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    priority_score: float = Field(ge=0, le=100)
    status: AlertStatus
    indicators: list[IndicatorReading]
    explanation: Explanation
    context: AlertContext
    evidence: Evidence
    disclaimer: str
    # Additions beyond the mock (never renamed, only added):
    first_observed_on: date
    n_observations: int
    peak_priority_score: float
    peak_severity: Severity
    model_version: str
    timeline: list[TimelineEntry] = Field(default_factory=list)
    updated_at: datetime


class AlertListItem(BaseModel):
    """GET /api/v1/alerts - one row of the priority-descending list."""

    alert_id: str
    water_body: WaterBodyRef
    zone: ZoneRef
    observed_on: date
    primary_indicator: str
    severity: Severity
    confidence: float
    priority_score: float
    status: AlertStatus
    natural_cause_likely: bool
    affected_area_km2: float | None
    summary: str
    n_observations: int
    disclaimer: str


class AlertList(BaseModel):
    items: list[AlertListItem]
    total: int
    disclaimer: str


class GeoJSONFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: dict[str, Any]
    properties: dict[str, Any]


class GeoJSONFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoJSONFeature]
    disclaimer: str


class AlertStatusUpdate(BaseModel):
    status: AlertStatus
    note: str | None = Field(default=None, max_length=2000)
    by: str | None = Field(default=None, max_length=200)
