"""Live IoT buoy telemetry (Khadakwasla pilot): a proxy for the hardware's own
HTTP endpoint on the local network, plus a GPT reading of its last payload.

Both endpoints are deliberately forgiving: the buoy being offline (powered
down, off the hotspot, mid-reboot) is an everyday, expected state here, not
an error worth a 500 -- ``GET /iot/live`` always answers 200 and says so.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.schemas.iot import IotAnalyzeRequest, IotAnalyzeResponse, IotLiveResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/iot", tags=["iot"])

OFFLINE_MESSAGE = "IoT sensor buoy offline or unreachable on local network (192.168.137.204)"

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
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(settings.iot_endpoint)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.info("iot buoy unreachable", extra={"endpoint": settings.iot_endpoint, "error": str(exc)})
        return IotLiveResponse(online=False, status="offline", data=None, error=OFFLINE_MESSAGE)
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
