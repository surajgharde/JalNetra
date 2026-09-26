"""Telegram push notifications for alerts (Phase 2, Layer 2): a crisp alert
card, with the investigation brief attached as a PDF when one is available.
Hooked into the existing delivery orchestrator as a new "telegram" recipient
channel -- see :func:`app.services.l12_delivery.dispatch.send_telegram`,
whose ``Recipient.target`` holds the chat id for that channel.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from app.schemas.alerts import AlertOut
from app.telegram.tools import INDICATOR_LABELS

log = logging.getLogger(__name__)

ALERT_CARD = """\
🚨 JALNETRA WATER QUALITY ALERT 🚨
━━━━━━━━━━━━━━━━━━━━━━
📍 Water Body: {water_body_name} ({zone_name})
📅 Date: {observed_on}
⚠️ Trigger: {indicator} ({z_score_str} above baseline)
📊 Severity: {severity} (Priority: {priority_score:.0f}/100)
💡 Recommendation: Deploy IoT sensor probe or inspect zone.
🔗 Dashboard: {dashboard_url}
"""


def format_alert_card(alert_data: dict[str, Any]) -> str:
    return ALERT_CARD.format(
        water_body_name=alert_data["water_body_name"],
        zone_name=alert_data["zone_name"],
        observed_on=alert_data["observed_on"],
        indicator=alert_data["indicator"],
        z_score_str=alert_data["z_score_str"],
        severity=str(alert_data["severity"]).upper(),
        priority_score=float(alert_data["priority_score"]),
        dashboard_url=alert_data["dashboard_url"],
    )


def alert_data_from_payload(payload: AlertOut, *, dashboard_base_url: str) -> dict[str, Any]:
    """The same fields :func:`format_alert_card` needs, pulled from the
    frozen ``AlertOut`` contract (the payload every other channel already
    sends) rather than re-querying anything."""
    key = payload.primary_indicator
    reading = next((r for r in payload.indicators if r.key == key), None)
    z = reading.z_score if reading else None
    return {
        "water_body_id": payload.water_body.id,
        "water_body_name": payload.water_body.name,
        "zone_name": payload.zone.name,
        "observed_on": payload.observed_on.isoformat(),
        "indicator": INDICATOR_LABELS.get(key, key),
        "z_score_str": f"{z:+.1f}σ" if z is not None else "n/a",
        "severity": payload.severity,
        "priority_score": payload.priority_score,
        "dashboard_url": f"{dashboard_base_url.rstrip('/')}/?wb={payload.water_body.id}",
        "brief_url": f"/api/v1/alerts/{payload.alert_id}/brief.pdf",
    }


async def send_telegram_alert(
    bot_token: str,
    chat_id: str,
    alert_data: dict[str, Any],
    brief_pdf_path: str | None = None,
) -> str:
    """Sends the formatted alert card to ``chat_id``, then the investigation
    brief as a document when ``brief_pdf_path`` is given -- a local file path,
    or an http(s) URL (Telegram fetches those server-side, so the brief
    stored in object storage can be attached by its own URL without this
    process downloading it first). Returns a short status string, mirroring
    ``send_webhook``/``send_email`` in dispatch.py."""
    text = format_alert_card(alert_data)
    bot = Bot(token=bot_token)
    try:
        async with bot:
            try:
                # Water body / zone names and ids often carry underscores,
                # which legacy Markdown reads as unmatched italic markers and
                # rejects outright (BadRequest) -- fall back to plain text
                # rather than losing the alert over a formatting quirk.
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest:
                await bot.send_message(chat_id=chat_id, text=text)
            if brief_pdf_path:
                if brief_pdf_path.startswith(("http://", "https://")):
                    await bot.send_document(chat_id=chat_id, document=brief_pdf_path, filename="brief.pdf")
                elif Path(brief_pdf_path).is_file():
                    with open(brief_pdf_path, "rb") as f:
                        await bot.send_document(chat_id=chat_id, document=f, filename=Path(brief_pdf_path).name)
    except TelegramError as exc:
        raise RuntimeError(f"telegram send to {chat_id} failed: {exc}") from exc
    return "sent"


def send_telegram_alert_sync(
    bot_token: str,
    chat_id: str,
    alert_data: dict[str, Any],
    brief_pdf_path: str | None = None,
) -> str:
    """Sync wrapper for callers not already inside an event loop -- namely
    dispatch.py, invoked synchronously from a Celery task."""
    return asyncio.run(send_telegram_alert(bot_token, chat_id, alert_data, brief_pdf_path))
