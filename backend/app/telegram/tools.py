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


# One pooled client for the process instead of a new TCP connection per call --
# a real (if modest, since this is all localhost) latency win, and the honest
# version of "speed up the HTTP calls": see the module docstring addendum
# below for why this stays HTTP-to-the-API rather than raw DB/GEE access.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=API_BASE_URL, timeout=_TIMEOUT_S)
    return _client


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
    r = await _http().get(path, params=params)
    r.raise_for_status()
    return r.json()


@_retry
async def _post(path: str, body: dict[str, Any]) -> Any:
    r = await _http().post(path, json=body, headers=_headers())
    r.raise_for_status()
    return r.json()


async def fetch_thumbnail_bytes(water_body_id: str, observed_on: str) -> bytes | None:
    """Raw PNG bytes for a lake's true-colour Sentinel-2 thumbnail on a date,
    fetched from this same machine (reachable) rather than handed to Telegram
    as a URL for its servers to fetch (which would only work if the API were
    publicly reachable -- it isn't, in dev). ``None`` on any failure (GEE off,
    no pass that day, network) so the caller can fall back to a text-only
    reply instead of erroring out."""
    try:
        r = await _http().get(
            "/imagery/thumbnail", params={"water_body_id": water_body_id, "date": observed_on}
        )
        if r.status_code >= 400:
            return None
        return r.content
    except httpx.HTTPError as exc:
        log.warning("thumbnail fetch failed", extra={"water_body_id": water_body_id, "error": str(exc)})
        return None


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
    return await _health_for(wb)


async def get_lake_health_by_id(water_body_id: str) -> dict[str, Any]:
    """Same as :func:`get_lake_health`, but for a caller (the /list and /saved
    inline-keyboard buttons) that already has the exact id and shouldn't pay
    for a name re-match."""
    try:
        wb = await _get(f"/water-bodies/{water_body_id}")
    except httpx.HTTPError as exc:
        return {"error": f"Could not load {water_body_id!r}: {exc}"}
    return await _health_for(wb)


async def _health_for(wb: dict[str, Any]) -> dict[str, Any]:
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

# Same quick-fetch window and relaxed cloud threshold as the dashboard's own
# "Fetch satellite data" button (FetchSatelliteButton.tsx) -- 5 days and the
# default 60% cloud cutoff too often found nothing at all over India.
TRIGGER_SCAN_WINDOW_DAYS = 35
TRIGGER_SCAN_MAX_CLOUD_PCT = 95


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
                "max_cloud_pct": TRIGGER_SCAN_MAX_CLOUD_PCT,
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


async def list_wishlist() -> list[dict[str, Any]]:
    """The dashboard's saved-water-bodies list (one shared wishlist -- the app
    has no per-user accounts), for the bot's /saved command and the "View
    Wishlist" inline button."""
    data = await _get("/wishlist")
    return data.get("items", [])


# --- IoT sensor buoy (in-situ telemetry) ---------------------------------------------
#
# The satellite tools above infer water quality from reflectance; these two read the
# pilot buoy's own probes in the water. Keep the distinction visible to GPT: a
# satellite indicator is a proxy, an in-situ probe is a direct measurement, and the
# two disagreeing is information rather than a fault.

# The buoy is field hardware on a local hotspot: powered down, out of range or
# mid-reboot are everyday states, so "offline" is data, never an error to apologise
# for. /iot/live already answers 200 with online:false; these wrappers preserve that.
IOT_WATER_BODY_NAME = os.getenv("IOT_WATER_BODY_NAME", "Khadakwasla Dam")


def _flatten_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    """The buoy's nested payload as flat, labelled scalars.

    Tolerates whichever fields a given firmware sent -- the hardware payload is
    deliberately not part of the frozen API contract (see app/schemas/iot.py), so a
    missing key means "this firmware didn't report it", not a bug.
    """
    temp = payload.get("temperature") or {}
    tds = payload.get("tds") or {}
    turb = payload.get("turbidity") or {}
    wifi = payload.get("wifi") or {}
    ts_ms = payload.get("timestamp_ms")
    reported_at: str | None = None
    if isinstance(ts_ms, (int, float)) and ts_ms > 0:
        # Firmware sends milliseconds; some builds send device uptime rather than a
        # wall clock, so an absurd date here is the device's, not ours to correct.
        try:
            reported_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            reported_at = None
    return {
        "online": True,
        "device": payload.get("device") or "unknown",
        # The firmware's own verdict on the water, when it offers one, and how the
        # reading reached us ("html" = scraped from the device's dashboard page,
        # because that firmware serves no JSON).
        "device_status": payload.get("device_status"),
        "source": payload.get("source") or "json",
        "reported_at": reported_at,
        "timestamp_ms": ts_ms,
        "temperature": temp.get("value"),
        "temperature_unit": temp.get("unit") or "°C",
        "tds": tds.get("value"),
        "tds_unit": tds.get("unit") or "ppm",
        "tds_adc": tds.get("adc"),
        "tds_voltage": tds.get("voltage"),
        "turbidity": turb.get("value"),
        "turbidity_unit": turb.get("unit") or "NTU",
        "turbidity_adc": turb.get("adc"),
        "turbidity_voltage": turb.get("voltage"),
        "turbidity_status": turb.get("status"),
        "wifi_rssi": wifi.get("rssi"),
        "wifi_ip": wifi.get("ip"),
    }


async def get_device_telemetry() -> dict[str, Any]:
    """Live in-situ reading from the IoT sensor buoy, or why it is unavailable."""
    try:
        live = await _get("/iot/live")
    except Exception as exc:  # backend down is different from buoy down; say which
        log.info("iot live fetch failed", extra={"error": str(exc)})
        return {"online": False, "error": f"Could not reach the JalNetra backend: {exc}"}
    if not live.get("online"):
        return {"online": False, "error": live.get("error") or "Sensor buoy offline."}
    return _flatten_telemetry(live.get("data") or {})


async def analyze_device_telemetry(water_body_name: str = "") -> dict[str, Any]:
    """An AI limnologist's reading of the buoy's current sample.

    A snapshot opinion on one telemetry sample -- not a scored finding, not persisted,
    and deliberately not fed into the alerting pipeline (see app/api/v1/iot.py).
    """
    try:
        live = await _get("/iot/live")
    except Exception as exc:
        log.info("iot live fetch failed", extra={"error": str(exc)})
        return {"online": False, "error": f"Could not reach the JalNetra backend: {exc}"}
    if not live.get("online"):
        return {"online": False, "error": live.get("error") or "Sensor buoy offline."}
    body = {
        "water_body_name": water_body_name or IOT_WATER_BODY_NAME,
        "telemetry": live.get("data") or {},
    }
    try:
        analysis = await _post("/iot/analyze", body)
    except Exception as exc:
        log.warning("iot analyze failed", extra={"error": str(exc)})
        return {"online": True, "error": f"AI analysis unavailable: {exc}"}
    return {
        "online": True,
        "water_body": body["water_body_name"],
        "telemetry": _flatten_telemetry(live.get("data") or {}),
        "analysis": analysis,
    }


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
    {
        "type": "function",
        "function": {
            "name": "get_device_telemetry",
            "description": (
                "Reads the LIVE in-situ IoT sensor buoy deployed at the Khadakwasla "
                "pilot site: water temperature, TDS (dissolved solids, ppm), turbidity "
                "(NTU) with raw ADC/voltage, sensor health and WiFi signal. Use this "
                "for any question about measured, current, real-time or in-water "
                "readings from the device/buoy/sensor -- these are direct probe "
                "measurements, unlike the satellite indicators which are proxies. "
                "Returns online:false when the buoy is unreachable, which is a normal "
                "field state."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_device_telemetry",
            "description": (
                "Expert limnologist interpretation of the sensor buoy's current "
                "reading: what the temperature, TDS and turbidity mean together, "
                "actionable suggestions for authorities, and a 7-14 day outlook. Use "
                "when the user asks what the device readings mean, whether the water "
                "is safe, or what to do about them -- not for the raw numbers alone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "water_body_name": {
                        "type": "string",
                        "description": (
                            "Water body the buoy sits in. Defaults to the configured "
                            "pilot site when omitted."
                        ),
                    },
                },
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "discover_lakes": discover_lakes,
    "get_lake_health": get_lake_health,
    "get_active_alerts": get_active_alerts,
    "trigger_scan": trigger_scan,
    "get_device_telemetry": get_device_telemetry,
    "analyze_device_telemetry": analyze_device_telemetry,
}
