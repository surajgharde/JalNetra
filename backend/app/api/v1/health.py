from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST

from app import __version__
from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.health import core_is_healthy, run_health_checks
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def health(response: Response, settings: Settings = Depends(get_settings)) -> HealthResponse:
    services = await run_health_checks(settings)
    all_ok = all(s.status == "ok" for s in services.values())
    # 503 only when a core dependency is down. A remote satellite source that blips
    # still reports "degraded" in the body -- the UI says which -- but must not make
    # a load balancer depool an instance that can still serve almost every endpoint.
    if not core_is_healthy(services):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        app=settings.app_name,
        version=__version__,
        env=settings.app_env,
        services=services,
    )


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus scrape target for the API process (workers serve their own)."""
    return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)
