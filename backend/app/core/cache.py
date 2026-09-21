"""Small async Redis JSON cache for read endpoints (S9).

Keys always embed the *latest scene id* of the water body (the caller passes
it), so a fresh scene changes the key and the stale entry simply expires;
there is no invalidation to get wrong. Every failure is fail-open: a Redis
outage makes the API slower, never broken.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

PREFIX = "jalnetra:api:"


@lru_cache
def get_redis() -> aioredis.Redis:
    client: aioredis.Redis = aioredis.Redis.from_url(
        get_settings().redis_url, decode_responses=True
    )
    return client


def cache_key(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return PREFIX + hashlib.sha1(raw.encode()).hexdigest()


async def cached_json(
    key: str,
    producer: Callable[[], Awaitable[Any]],
    *,
    settings: Settings | None = None,
) -> tuple[Any, bool]:
    """(value, hit). ``producer`` builds the JSON-serialisable value on a miss."""
    settings = settings or get_settings()
    if not settings.api_cache_enabled:
        return await producer(), False
    try:
        hit = await get_redis().get(key)
        if hit is not None:
            return json.loads(hit), True
    except Exception as exc:
        log.warning("cache read failed", extra={"error": str(exc)})
    value = await producer()
    try:
        await get_redis().set(key, json.dumps(value, default=str), ex=settings.api_cache_ttl_s)
    except Exception as exc:
        log.warning("cache write failed", extra={"error": str(exc)})
    return value, False


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
        get_redis.cache_clear()
