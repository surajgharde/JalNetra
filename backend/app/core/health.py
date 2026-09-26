"""Dependency health probes used by GET /health."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

import httpx
import redis.asyncio as aioredis
from minio import Minio
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import get_engine
from app.schemas.health import ServiceStatus

Check = Callable[[Settings], Awaitable[None]]


async def check_postgres(settings: Settings) -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis(settings: Settings) -> None:
    client: aioredis.Redis = aioredis.Redis.from_url(settings.redis_url)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def check_minio(settings: Settings) -> None:
    def _probe() -> None:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        client.bucket_exists(settings.minio_bucket)

    await asyncio.to_thread(_probe)


async def check_stac(settings: Settings) -> None:
    """The configured STAC source answers its landing page (no search, no auth).
    With Earth Engine as the primary source the STAC probe covers the fallback."""
    url = settings.cdse_stac_url if settings.stac_source == "cdse" else settings.earth_search_url
    async with httpx.AsyncClient(timeout=settings.health_check_timeout_s) as client:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()


async def check_gee(settings: Settings) -> None:
    """Earth Engine session initialises and answers one trivial computation."""
    from app.services.l03_ingestion.gee import ping

    await asyncio.to_thread(ping, settings)


CHECKS: dict[str, Check] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "minio": check_minio,
    "stac": check_stac,
}
# Probes that leave the machine, and so get health_remote_timeout_s instead of the
# tighter local ceiling. A slow internet link is not a degraded service.
#
# They are also the probes that must not depool the API: everything except live
# imagery and fresh ingestion reads the database, so the instance still serves most
# of its traffic when a satellite source is unreachable, and there is no healthier
# instance to fail over to. Anything NOT listed here counts as core -- a new local
# dependency is load-bearing until someone says otherwise.
REMOTE_CHECKS: frozenset[str] = frozenset({"stac", "gee"})


def core_is_healthy(services: Mapping[str, ServiceStatus]) -> bool:
    """True when every dependency the API cannot serve without is answering.

    Drives the HTTP status: 503 means "take this instance out of rotation", which a
    blip on a remote satellite source does not warrant. Those still show up as
    ``degraded`` in the body so the UI can say what is impaired.
    """
    return all(s.status == "ok" for name, s in services.items() if name not in REMOTE_CHECKS)


# Probes that only run when the feature is switched on.
OPTIONAL_CHECKS: dict[str, tuple[Callable[[Settings], bool], Check]] = {
    "gee": (lambda s: s.gee_enabled, check_gee),
}


def _timeout_for(name: str, settings: Settings) -> float:
    if name in REMOTE_CHECKS:
        return max(settings.health_remote_timeout_s, settings.health_check_timeout_s)
    return settings.health_check_timeout_s


async def _run(name: str, check: Check, settings: Settings) -> ServiceStatus:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check(settings), timeout=_timeout_for(name, settings))
        return ServiceStatus(status="ok", latency_ms=_elapsed_ms(started))
    except TimeoutError:
        return ServiceStatus(
            status="error",
            latency_ms=_elapsed_ms(started),
            error=f"timeout after {_timeout_for(name, settings):g}s",
        )
    except Exception as exc:  # a probe must never raise out of /health
        return ServiceStatus(
            status="error",
            latency_ms=_elapsed_ms(started),
            error=f"{type(exc).__name__}: {exc}",
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def run_health_checks(settings: Settings) -> dict[str, ServiceStatus]:
    checks = dict(CHECKS)
    checks.update({n: c for n, (enabled, c) in OPTIONAL_CHECKS.items() if enabled(settings)})
    names = list(checks)
    results = await asyncio.gather(*(_run(n, checks[n], settings) for n in names))
    return dict(zip(names, results, strict=True))
