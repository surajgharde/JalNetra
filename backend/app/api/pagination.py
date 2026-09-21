"""Opaque keyset cursors for list endpoints (S9).

A cursor is the base64 of the JSON tuple of the sort keys of the last row
returned. Keyset pagination stays O(1) as the table grows and never skips or
repeats rows while new alerts arrive, which offset pagination does.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import HTTPException, status


def encode_cursor(*keys: Any) -> str:
    raw = json.dumps(list(keys), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, n: int) -> list[Any] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        keys = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc
    if not isinstance(keys, list) or len(keys) != n:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor")
    return keys
