"""Live IoT buoy telemetry (Khadakwasla pilot): a proxy for the hardware's own
HTTP endpoint on the local network, plus a GPT reading of its last payload.

Both endpoints are deliberately forgiving: the buoy being offline (powered
down, off the hotspot, mid-reboot) is an everyday, expected state here, not
an error worth a 500 -- ``GET /iot/live`` always answers 200 and says so.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.schemas.iot import IotAnalyzeRequest, IotAnalyzeResponse, IotLiveResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/iot", tags=["iot"])

# The pilot ESP32 firmware exposes no JSON API at all -- it serves one HTML dashboard
# (meta-refresh every 2 s) with the readings rendered into the page. Reading the device
# therefore means parsing that page, rather than blocking on a firmware release. Newer
# firmware that does serve JSON keeps working unchanged: whichever the device returns
# is normalised to the same payload shape.
_LABEL_VALUE_RE = re.compile(
    r'class="label"[^>]*>(?P<label>.*?)</div>\s*<div[^>]*class="value"[^>]*>(?P<value>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_STATUS_RE = re.compile(r'class="status"[^>]*>(?P<status>.*?)</div>', re.IGNORECASE | re.DOTALL)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_TAG_RE = re.compile(r"<[^>]+>")

# Card labels the dashboard is known to render, mapped onto the JSON contract's keys.
# Anything else is passed through under its own slug, so a firmware that adds a card
# shows up instead of being silently dropped.
_LABEL_KEYS = {
    "TURBIDITY": "turbidity",
    "TDS": "tds",
    "TEMPERATURE": "temperature",
    "TEMP": "temperature",
    "PH": "ph",
}


def _text(fragment: str) -> str:
    return " ".join(_TAG_RE.sub(" ", fragment).split())


def _parse_device_html(html: str) -> dict[str, Any] | None:
    """The buoy's HTML dashboard as a telemetry payload, or None if it isn't one.

    Units are taken verbatim from the page and never converted: this firmware reports
    turbidity as a percentage, which is not NTU and has no calibration here to become
    NTU. Relabelling it would invent precision the device never claimed.
    """
    payload: dict[str, Any] = {"device": "JAL NETRA ESP32", "source": "html"}
    for match in _LABEL_VALUE_RE.finditer(html):
        label = _text(match.group("label")).upper()
        raw = _text(match.group("value"))
        number = _NUMBER_RE.search(raw)
        if not label or number is None:
            continue
        entry: dict[str, Any] = {"value": float(number.group())}
        unit = raw[number.end() :].strip()
        if unit:
            entry["unit"] = unit
        payload[_LABEL_KEYS.get(label, label.lower().replace(" ", "_"))] = entry
    if len(payload) <= 2:  # nothing but the two metadata keys -- not a reading page
        return None
    status = _STATUS_RE.search(html)
    if status:
        # The page's own verdict on the water. Deliberately NOT turbidity.status,
        # which in the JSON contract means the health of that one probe.
        text = re.sub(r"^STATUS\s*:\s*", "", _text(status.group("status")), flags=re.IGNORECASE)
        if text.strip():
            payload["device_status"] = text.strip()
    return payload


def _payload_from_response(body: str, content_type: str) -> dict[str, Any] | None:
    """Normalise whatever the hardware answered into one payload shape."""
    text = body.strip()
    if not text:
        return None
    if "json" in content_type.lower() or text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if "<" in text:
        return _parse_device_html(text)
    return None


def _offline_message(settings: Settings) -> str:
    """Name the address we actually tried, not a hardcoded one.

    This used to pin a literal IP that drifted out of step with IOT_ENDPOINT, so the
    error told operators the buoy was unreachable at an address nothing had contacted.
    """
    host = urlsplit(settings.iot_endpoint).hostname or settings.iot_endpoint
    return f"IoT sensor buoy offline or unreachable on local network ({host})"

SYSTEM_PROMPT = (
    "You are the JalNetra Senior Limnologist and Water Intelligence AI. You analyze "
    "in-situ IoT telemetry from reservoir buoys (temperature, turbidity voltage/NTU, "
    "TDS ppm, sensor health) combined with satellite intelligence for water bodies in "
    "India."
)

_openai_client: Any | None = None


def _openai(settings: Settings) -> Any:
    """One client reused across requests, matching the Telegram bot's own
    pattern -- keyed on nothing per-request since there's one API key."""
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


RESPONSE_SHAPE = """Respond with ONLY a JSON object, no prose outside it, in exactly this shape:
{
  "condition_summary": "<Concise 2-3 sentence overview of water condition>",
  "parameters": {
    "temperature_analysis": "<Analysis of thermal reading and stratification>",
    "tds_analysis": "<Analysis of dissolved solids and salinity/potability>",
    "turbidity_analysis": "<Analysis of light penetration, suspended solids, and sensor voltage>"
  },
  "suggestions": [
    "<Actionable step 1 for irrigation / municipal authorities>",
    "<Actionable step 2 for aeration / filtration / patrol>",
    "<Actionable step 3 for catchment monitoring>"
  ],
  "future_prediction": "<7-14 day forecast on how seasonal shifts/temperature/turbidity will evolve>"
}"""


@router.get("/live", response_model=IotLiveResponse)
async def iot_live(settings: Annotated[Settings, Depends(get_settings)]) -> IotLiveResponse:
    """Polls the buoy's own endpoint right now. Never raises for an offline
    buoy -- that's the normal state of a piece of field hardware, reported as
    ``online: false`` with a 200, so the frontend can show it gracefully
    instead of an error boundary."""
    try:
        async with httpx.AsyncClient(timeout=settings.iot_timeout_s) as client:
            r = await client.get(settings.iot_endpoint)
            r.raise_for_status()
            body, content_type = r.text, r.headers.get("content-type", "")
    except Exception as exc:
        log.info("iot buoy unreachable", extra={"endpoint": settings.iot_endpoint, "error": str(exc)})
        return IotLiveResponse(
            online=False, status="offline", data=None, error=_offline_message(settings)
        )
    data = _payload_from_response(body, content_type)
    if data is None:
        # The device answered, so it is not offline -- but nothing in the reply looks
        # like a reading. Say that precisely instead of blaming the network.
        log.warning(
            "iot payload not understood",
            extra={"endpoint": settings.iot_endpoint, "content_type": content_type},
        )
        return IotLiveResponse(
            online=False,
            status="offline",
            data=None,
            error=(
                f"The device at {urlsplit(settings.iot_endpoint).hostname} answered, but "
                "its reply carried no readings this app could read."
            ),
        )
    return IotLiveResponse(
        online=True,
        status="online",
        data=data,
        fetched_at=datetime.now(UTC).isoformat(),
    )


def _telemetry_lines(t: dict[str, Any]) -> str:
    """Pull just the fields the prompt asks for out of the buoy's raw payload,
    tolerating whichever ones a given firmware happened to send."""
    temp = t.get("temperature") or {}
    tds = t.get("tds") or {}
    turb = t.get("turbidity") or {}
    wifi = t.get("wifi") or {}
    lines = [
        f"Device: {t.get('device', 'unknown')}",
        f"Temperature: {temp.get('value')} {temp.get('unit', '')}".strip(),
        f"TDS: {tds.get('value')} {tds.get('unit', '')} (ADC {tds.get('adc')}, {tds.get('voltage')} V)",
        f"Turbidity: {turb.get('value')} {turb.get('unit', '')} "
        f"(ADC {turb.get('adc')}, status: {turb.get('status', 'unknown')})",
        f"WiFi signal: {wifi.get('rssi')} dBm",
    ]
    return "\n".join(lines)


@router.post("/analyze", response_model=IotAnalyzeResponse)
async def iot_analyze(
    body: IotAnalyzeRequest, settings: Annotated[Settings, Depends(get_settings)]
) -> IotAnalyzeResponse:
    """GPT's read of one buoy payload -- a snapshot opinion on a single
    telemetry sample, not a scored finding; nothing here is persisted or fed
    into the alerting pipeline."""
    if not settings.openai_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured")
    client = _openai(settings)
    user_prompt = (
        f"Water body: {body.water_body_name}\n\n"
        f"Live buoy telemetry:\n{_telemetry_lines(body.telemetry)}\n\n{RESPONSE_SHAPE}"
    )
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        return IotAnalyzeResponse.model_validate(parsed)
    except Exception as exc:
        log.warning("iot analyze failed", extra={"error": str(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI analysis failed: {exc}") from exc
