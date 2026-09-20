from typing import Literal

from pydantic import BaseModel, Field


class ServiceStatus(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float = Field(ge=0)
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    env: str
    services: dict[str, ServiceStatus]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "ok",
                    "app": "JalNetra",
                    "version": "0.1.0",
                    "env": "dev",
                    "services": {
                        "postgres": {"status": "ok", "latency_ms": 3.1, "error": None},
                        "redis": {"status": "ok", "latency_ms": 0.8, "error": None},
                        "minio": {"status": "ok", "latency_ms": 5.4, "error": None},
                    },
                }
            ]
        }
    }
