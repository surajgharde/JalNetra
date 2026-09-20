"""Dependency health probes used by GET /health."""

import asyncio
import time
from collections.abc import Awaitable, Callable

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


CHECKS: dict[str, Check] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "minio": check_minio,
}


async def _run(name: str, check: Check, settings: Settings) -> ServiceStatus:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check(settings), timeout=settings.health_check_timeout_s)
        return ServiceStatus(status="ok", latency_ms=_elapsed_ms(started))
    except TimeoutError:
        return ServiceStatus(status="error", latency_ms=_elapsed_ms(started), error="timeout")
    except Exception as exc:  # a probe must never raise out of /health
        return ServiceStatus(
            status="error",
            latency_ms=_elapsed_ms(started),
            error=f"{type(exc).__name__}: {exc}",
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def run_health_checks(settings: Settings) -> dict[str, ServiceStatus]:
    names = list(CHECKS)
    results = await asyncio.gather(*(_run(n, CHECKS[n], settings) for n in names))
    return dict(zip(names, results, strict=True))
