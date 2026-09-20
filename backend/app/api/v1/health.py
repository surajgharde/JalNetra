from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.core.config import Settings, get_settings
from app.core.health import run_health_checks
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
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        app=settings.app_name,
        version=__version__,
        env=settings.app_env,
        services=services,
    )
