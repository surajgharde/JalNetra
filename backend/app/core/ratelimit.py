"""Per-client rate limiting (S9) via slowapi. Limits are read from settings;
storage is in-memory per process, which is what a two-replica pilot needs.
Switch to ``storage_uri=settings.redis_url`` when the API scales out."""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[get_settings().rate_limit_default],
    headers_enabled=True,
)
