import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import __version__
from app.api.middleware import RequestIdMiddleware
from app.api.router import api_router, v1_router
from app.core.cache import close_redis
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.observability import init_sentry
from app.core.ratelimit import limiter
from app.db.session import dispose_engine

log = logging.getLogger(__name__)

DESCRIPTION = (
    "Satellite-based water quality anomaly intelligence for Maharashtra.\n\n"
    "**Boundary:** the system detects *potential anomalies* in satellite-observable "
    "indicators and prioritises zones for ground investigation. It never claims to "
    "detect, prove or confirm pollution or contamination."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_sentry("api", settings=settings)
    log.info("startup", extra={"env": settings.app_env, "version": __version__})
    yield
    await close_redis()
    await dispose_engine()
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)
    # Per-client rate limiting (settings.rate_limit_default; tiles have their own).
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(api_router)
    app.include_router(v1_router)
    return app


app = create_app()
