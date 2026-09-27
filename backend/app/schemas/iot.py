"""IoT buoy telemetry (Khadakwasla pilot): a thin, honest proxy for one piece
of local-network hardware, plus an AI reading of whatever it last reported.

The telemetry payload shape is the hardware's own and not part of the frozen
API contract elsewhere in this app -- it's passed through as a free-form
object rather than pinned field-by-field, since a firmware change shouldn't
require a backend release.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class IotLiveResponse(BaseModel):
    online: bool
    status: Literal["online", "offline"]
    data: dict[str, Any] | None = Field(default=None, description="raw hardware payload, passed through")
    fetched_at: str | None = None
    error: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "online": False,
                    "status": "offline",
                    "data": None,
                    "fetched_at": None,
                    "error": "IoT sensor buoy offline or unreachable on local network (192.168.137.204)",
                }
            ]
        }
    )


class IotAnalyzeRequest(BaseModel):
    water_body_name: str = Field(default="Khadakwasla Dam", max_length=200)
    telemetry: dict[str, Any] = Field(description="the buoy's own payload, as returned by /iot/live")


class IotAnalyzeResponse(BaseModel):
    condition_summary: str
    parameters: dict[str, str]
    suggestions: list[str]
    future_prediction: str
