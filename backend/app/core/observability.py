"""Sentry (S12). Initialised once per process when ``SENTRY_DSN`` is set;
otherwise every call here is a no-op, so a laptop never needs the SDK
configured. Tasks tag events with the water body and scene they were
processing (``app.workers.signals``)."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)
_initialised = False


def init_sentry(role: str, *, settings: Settings | None = None) -> bool:
    """``role`` is "api" or "worker:<queues>"; returned True when the SDK started."""
    global _initialised
    settings = settings or get_settings()
    if _initialised or not settings.sentry_dsn:
        return _initialised
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:  # optional extra not installed
        log.warning("SENTRY_DSN set but sentry-sdk is not installed (uv sync --extra ops)")
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        release=f"jalnetra@{settings.app_version}",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[CeleryIntegration(), FastApiIntegration(), SqlalchemyIntegration()],
        send_default_pii=False,
    )
    sentry_sdk.set_tag("role", role)
    _initialised = True
    log.info("sentry initialised", extra={"role": role, "env": settings.app_env})
    return True


def set_tags(tags: dict[str, Any]) -> None:
    if not _initialised:
        return
    import sentry_sdk

    for k, v in tags.items():
        if v is not None:
            sentry_sdk.set_tag(k, str(v))


def clear_tags(keys: list[str]) -> None:
    if not _initialised:
        return
    import sentry_sdk

    scope = sentry_sdk.get_isolation_scope()
    for k in keys:
        scope.remove_tag(k)
