"""Pipeline job and validation schemas (S9)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["queued", "running", "done", "failed"]
STAGES: tuple[str, ...] = ("ingestion", "mask", "indicators", "anomalies", "scoring", "alerts")


class IngestJobRequest(BaseModel):
    water_body_id: str
    date_from: date
    date_to: date | None = Field(default=None, description="defaults to date_from")
    requested_by: str | None = Field(default=None, max_length=200)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "water_body_id": "wb_khadakwasla",
                    "date_from": "2026-09-01",
                    "date_to": "2026-09-21",
                }
            ]
        }
    )


class StageProgress(BaseModel):
    stage: str
    done: int
    total: int
    pct: float


class JobOut(BaseModel):
    job_id: str
    kind: str
    water_body_id: str
    date_from: date
    date_to: date
    status: JobStatus
    progress_pct: float = Field(ge=0, le=100)
    current_stage: str | None = Field(description="first stage with pending work; null when done")
    stages: list[StageProgress]
    scenes_found: int
    scenes_usable: int
    alerts_created: int
    celery_state: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "job_3f9c2b7e4a5d4c1e9b8a7f6e5d4c3b2a",
                    "kind": "ingest",
                    "water_body_id": "wb_khadakwasla",
                    "date_from": "2026-09-01",
                    "date_to": "2026-09-21",
                    "status": "running",
                    "progress_pct": 58.3,
                    "current_stage": "indicators",
                    "stages": [
                        {"stage": "ingestion", "done": 4, "total": 4, "pct": 100},
                        {"stage": "mask", "done": 4, "total": 4, "pct": 100},
                        {"stage": "indicators", "done": 2, "total": 4, "pct": 50},
                        {"stage": "anomalies", "done": 1, "total": 4, "pct": 25},
                        {"stage": "scoring", "done": 1, "total": 4, "pct": 25},
                        {"stage": "alerts", "done": 1, "total": 4, "pct": 25},
                    ],
                    "scenes_found": 5,
                    "scenes_usable": 4,
                    "alerts_created": 1,
                    "celery_state": "SUCCESS",
                    "error": None,
                    "created_at": "2026-09-21T10:00:00Z",
                    "updated_at": "2026-09-21T10:03:12Z",
                    "finished_at": None,
                }
            ]
        }
    )


class JobList(BaseModel):
    items: list[JobOut]
    next_cursor: str | None


class LabResults(BaseModel):
    turbidity_ntu: float | None = Field(default=None, ge=0)
    chlorophyll_ug_l: float | None = Field(default=None, ge=0)
    tss_mg_l: float | None = Field(default=None, ge=0)
    do_mg_l: float | None = Field(default=None, ge=0)
    ph: float | None = Field(default=None, ge=0, le=14)
    temperature_c: float | None = None
    conductivity_us_cm: float | None = Field(default=None, ge=0)


class ValidationIn(BaseModel):
    alert_id: str
    sampled_on: date
    lab_results: LabResults = Field(default_factory=LabResults)
    observed_condition: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=4000)
    submitted_by: str | None = Field(default=None, max_length=200)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "alert_id": "alr_2026_0917_khadakwasla_z3",
                    "sampled_on": "2026-09-19",
                    "lab_results": {"turbidity_ntu": 48.0, "tss_mg_l": 62.0, "ph": 7.6},
                    "observed_condition": "Brown plume near the eastern inlet, no odour.",
                    "submitted_by": "RO Pune field team",
                }
            ]
        }
    )


class ValidationOut(BaseModel):
    id: int
    alert_id: str
    sampled_on: date
    lab_results: dict[str, Any]
    observed_condition: str | None
    notes: str | None
    submitted_by: str | None
    verdict: Literal["matched", "not_matched", "inconclusive"] | None
    verdict_reason: str | None
    created_at: datetime


class ValidationList(BaseModel):
    items: list[ValidationOut]
    total: int
    next_cursor: str | None
