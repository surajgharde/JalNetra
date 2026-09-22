"""API-key authentication for write endpoints.

Keys are configured as ``API_KEYS='{"<key>": "<actor label>"}'``. The label
is what lands in audit columns (``alerts.status_by``, ``validations.submitted_by``,
``jobs.requested_by``) so an entry can never be forged by the caller.

Read endpoints stay open (the dashboard is a public-facing product surface);
every mutating endpoint depends on :func:`require_actor`.

With no keys configured the behaviour depends on the environment: ``dev`` and
``test`` allow writes as ``anonymous`` so a fresh clone works, ``prod`` refuses
every write with 503 so a misconfigured deployment fails loudly instead of open.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

API_KEY_HEADER = "X-API-Key"
_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


@dataclass(frozen=True)
class Actor:
    name: str
    authenticated: bool

    def label(self, fallback: str | None = None) -> str | None:
        """The audit label: the key's actor when authenticated, otherwise the
        caller-supplied fallback (dev only)."""
        return self.name if self.authenticated else (fallback or self.name)


def _lookup(key: str, keys: dict[str, str]) -> str | None:
    # Constant-time compare against every key so timing cannot leak a prefix.
    found: str | None = None
    for candidate, actor in keys.items():
        if hmac.compare_digest(candidate.encode(), key.encode()):
            found = actor
    return found


async def require_actor(
    api_key: Annotated[str | None, Security(_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Actor:
    keys = settings.api_keys
    if not keys:
        if settings.app_env == "prod":
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "write endpoints are disabled: no API_KEYS configured",
            )
        return Actor(name="anonymous", authenticated=False)
    if not api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"missing {API_KEY_HEADER} header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    actor = _lookup(api_key, keys)
    if actor is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid API key")
    return Actor(name=actor, authenticated=True)


ActorDep = Annotated[Actor, Depends(require_actor)]
