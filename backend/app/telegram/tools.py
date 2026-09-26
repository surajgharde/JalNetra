"""OpenAI function-calling tools (Phase 2, Layer 2): thin async wrappers over
JalNetra's own FastAPI backend, so GPT answers from the real registry,
indicators and alerts instead of guessing.

Each tool returns a small JSON-serializable dict -- never the raw API
response -- so GPT sees exactly the fields it needs to answer, not the
frozen contract's full shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)

API_BASE_URL = os.getenv("JALNETRA_API_BASE_URL", "http://localhost:8000/api/v1")
# Unset in dev, where write endpoints allow anonymous callers (app.core.auth);
# set it if the backend this bot talks to has API_KEYS configured.
API_KEY = os.getenv("JALNETRA_API_KEY")
_TIMEOUT_S = 20.0

INDICATOR_LABELS: dict[str, str] = {
    "ndti_turbidity": "Turbidity",
    "ndci_chlorophyll": "Chlorophyll",
    "fai_algal": "Algae",
    "sediment_proxy": "Sediment",
    "mndwi_extent": "Water extent",
}


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY} if API_KEY else {}


# Connection-level hiccups only -- an HTTP error status (raise_for_status)
# means the request was answered and retrying would just hit it again. The
# discovery endpoint in particular calls out to Nominatim/Overpass, both
# public, rate-limited, occasionally-slow services (see discovery.py).
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError)
_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    retry=retry_if_exception_type(_RETRYABLE),
)


@_retry
async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=_TIMEOUT_S) as client:
        r = await client.get(path, params=params)
        r.raise_for_status()
        return r.json()


@_retry
async def _post(path: str, body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(
        base_url=API_BASE_URL, timeout=_TIMEOUT_S, headers=_headers()
    ) as client:
        r = await client.post(path, json=body)
        r.raise_for_status()
        return r.json()


async def _find_water_body(name: str) -> dict[str, Any] | None:
    """Best-effort case-insensitive name match against the registry. There is
    no name filter on the list endpoint, so this mirrors the dashboard's own
    client-side search (WaterBodyBar.tsx): fetch the (small) registry and
    match locally -- exact match first, then substring."""
    needle = name.strip().lower()
    if not needle:
        return None
    data = await _get("/water-bodies", {"limit": 200})
    items: list[dict[str, Any]] = data.get("items", [])
    for it in items:
        if it["name"].strip().lower() == needle:
            return it
    for it in items:
        if needle in it["name"].lower():
            return it
    return None


def _reading_status(reading: dict[str, Any] | None) -> str:
    if not reading or reading.get("baseline_status") != "usable" or reading.get("z_score") is None:
        return "Building baseline"
    az = abs(reading["z_score"])
    return "Elevated" if az > 5 else "Watch" if az > 3 else "Normal"


def _worst_reading(zones: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for zone in zones:
        for r in zone.get("indicators", []):
            if r.get("key") != key:
                continue
            if best is None or abs(r.get("z_score") or 0) > abs(best.get("z_score") or 0):
                best = r
    return best


# --- Tool 1 -------------------------------------------------------------------------


async def discover_lakes(
    latitude: float, longitude: float, radius_km: float = 15.0
) -> dict[str, Any]:
    """Finds surface water bodies (lakes, reservoirs, rivers) near a GPS
    coordinate within radius_km, sorted by surface area."""
    try:
        data = await _post(
            "/water-bodies/discover",
            {"latitude": latitude, "longitude": longitude, "radius_km": radius_km},
        )
    except httpx.HTTPError as exc:
        log.warning("discover_lakes failed", extra={"error": str(exc)})
        return {"error": f"Could not reach the discovery service: {exc}"}
    # A placeholder is a "nothing mapped here" box, not a real lake (S14) --
    # never worth surfacing to a chat answer.
    items = [
        it for it in data.get("items", []) if not str(it.get("osm_id", "")).startswith("placeholder:")
    ]
    top = items[:5]
    return {
        "resolved_centre_lon_lat": data.get("centre"),
        "radius_km": data.get("radius_km"),
        "total_found": len(items),
        "lakes": [
            {
                "name": it["name"],
                "area_km2": it["area_km2"],
                "distance_km": it["distance_km"],
                "kind": it["kind"],
                "already_tracked": it.get("already_registered_id") is not None,
            }
            for it in top
        ],
    }


# --- Tool 2 -------------------------------------------------------------------------


async def get_lake_health(name: str) -> dict[str, Any]:
    """Retrieves current water quality indicators (turbidity NDTI, chlorophyll
    NDCI, algae FAI, water extent km2, cloud clear %) and active alerts for a
    specified lake."""
    wb = await _find_water_body(name)
    if wb is None:
        return {"error": f"No monitored water body matching {name!r}. Try /lakes to see what's tracked."}
    wb_id = wb["id"]
    try:
        indicators, observations = await asyncio.gather(
            _get(f"/water-bodies/{wb_id}/indicators"),
            _get(f"/water-bodies/{wb_id}/observations", {"limit": 1}),
        )
    except httpx.HTTPError as exc:
        return {"error": f"Could not load indicators for {wb['name']!r}: {exc}"}

    zones = indicators.get("zones", [])
    readings = {key: _worst_reading(zones, key) for key in ("ndti_turbidity", "ndci_chlorophyll", "fai_algal")}
    latest_obs = (observations.get("items") or [None])[0]

    anomaly_present = any(
        r is not None and r.get("baseline_status") == "usable" and abs(r.get("z_score") or 0) > 3
        for r in readings.values()
    )

    return {
        "water_body_id": wb_id,
        "name": wb["name"],
        "district": wb["district"],
        "status": wb["status"],
        "open_alerts": wb["open_alerts"],
        "max_open_severity": wb.get("max_open_severity"),
        "observed_on": indicators.get("observed_on"),
        "indicators": {
            INDICATOR_LABELS[key].lower(): {
                "value": r.get("value") if r else None,
                "z_score": r.get("z_score") if r else None,
                "status": _reading_status(r),
            }
            for key, r in readings.items()
        },
        "water_extent_km2": latest_obs.get("water_extent_km2") if latest_obs else None,
        "cloud_clear_pct": latest_obs.get("valid_pixel_pct") if latest_obs else None,
        "anomaly_present": anomaly_present,
    }


# --- Tool 3 -------------------------------------------------------------------------


async def _alert_trigger_label(alert_id: str) -> str | None:
    """The primary indicator's z-score for one alert, e.g. "Chlorophyll +3.8σ"
    -- not on the list endpoint, so this is a bounded (limit=5) N+1 detail
    fetch, only run when a user actually asks for the alert queue."""
    try:
        detail = await _get(f"/alerts/{alert_id}")
    except httpx.HTTPError:
        return None
    key = detail.get("primary_indicator")
    reading = next((r for r in detail.get("indicators", []) if r.get("key") == key), None)
    label = INDICATOR_LABELS.get(key, key)
    if reading and reading.get("z_score") is not None:
        return f"{label} {reading['z_score']:+.1f}σ"
    return label


async def get_active_alerts() -> dict[str, Any]:
    """Returns currently active high-priority water quality alerts across
    monitored water bodies."""
    try:
        data = await _get("/alerts", {"status": "open", "limit": 5})
    except httpx.HTTPError as exc:
        return {"error": f"Could not load alerts: {exc}"}
    items = data.get("items", [])
    triggers = await asyncio.gather(*(_alert_trigger_label(it["alert_id"]) for it in items))
    return {
        "total_open": data.get("total", len(items)),
        "alerts": [
            {
                "alert_id": it["alert_id"],
                "water_body": it["water_body"]["name"],
                "zone": it["zone"]["name"],
                "trigger": trigger,
                "severity": it["severity"],
                "priority_score": it["priority_score"],
                "observed_on": str(it["observed_on"]),
            }
            for it, trigger in zip(items, triggers, strict=True)
        ],
    }


# --- Tool 4 -------------------------------------------------------------------------

TRIGGER_SCAN_WINDOW_DAYS = 5  # same quick-fetch window as the dashboard's own button


async def trigger_scan(lake_name: str) -> dict[str, Any]:
    """Initiates a fast Sentinel-2 satellite scan for a specific water body."""
    wb = await _find_water_body(lake_name)
    if wb is None:
        return {"error": f"No monitored water body matching {lake_name!r}. Try /lakes to see what's tracked."}
    to = datetime.now(UTC).date()
    frm = to - timedelta(days=TRIGGER_SCAN_WINDOW_DAYS)
    try:
        job = await _post(
            "/jobs/ingest",
            {
                "water_body_id": wb["id"],
                "date_from": frm.isoformat(),
                "date_to": to.isoformat(),
                "requested_by": "telegram-bot",
                "max_scenes": 1,
            },
        )
    except httpx.HTTPError as exc:
        return {"error": f"Could not start a scan for {wb['name']!r}: {exc}"}
    return {
        "water_body": wb["name"],
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "message": f"Fetching the latest Sentinel-2 pass for {wb['name']} (job {job.get('job_id')}).",
    }


# --- Convenience (not a GPT tool -- used directly by /lakes) ------------------------


async def list_top_lakes(limit: int = 10) -> list[dict[str, Any]]:
    """Bodies with open alerts first, then by tier -- same ordering as the
    dashboard's water-body bar (WaterBodyBar.tsx)."""
    data = await _get("/water-bodies", {"limit": 200})
    items: list[dict[str, Any]] = data.get("items", [])
    items.sort(key=lambda wb: (wb["open_alerts"] == 0, wb["tier"], wb["name"]))
    return items[:limit]


# --- OpenAI tool schemas + dispatch table --------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "discover_lakes",
            "description": (
                "Finds surface water bodies (lakes, reservoirs, rivers) near a GPS "
                "coordinate within radius_km, sorted by surface area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number", "description": "GPS latitude, e.g. 18.4419"},
                    "longitude": {"type": "number", "description": "GPS longitude, e.g. 73.7712"},
                    "radius_km": {
                        "type": "number",
                        "description": "Search radius in kilometres.",
                        "default": 15.0,
                    },
                },
                "required": ["latitude", "longitude"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lake_health",
            "description": (
                "Retrieves current water quality indicators (turbidity NDTI, "
                "chlorophyll NDCI, algae FAI, water extent km2, cloud clear %) and "
                "active alerts for a specified lake."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Lake, reservoir or river name."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_alerts",
            "description": (
                "Returns currently active high-priority water quality alerts across "
                "monitored water bodies."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_scan",
            "description": "Initiates a fast Sentinel-2 satellite scan for a specific water body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lake_name": {"type": "string", "description": "Lake, reservoir or river name."},
                },
                "required": ["lake_name"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "discover_lakes": discover_lakes,
    "get_lake_health": get_lake_health,
    "get_active_alerts": get_active_alerts,
    "trigger_scan": trigger_scan,
}
