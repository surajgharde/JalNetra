"""JalNetra Telegram bot (Phase 2): a GPT-4o-mini assistant, grounded in the
platform's own live data through OpenAI function calling (Layer 2) -- lake
lookup, indicator health, active alerts and satellite-scan triggers, plus
native GPS location sharing for "what's near me".

Run it directly (polling, no webhook/ngrok needed):

    cd backend && uv run python -m app.telegram.bot
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.core.logging import configure_logging
from app.telegram.tools import (
    TOOL_FUNCTIONS,
    TOOLS,
    analyze_device_telemetry,
    discover_lakes,
    fetch_thumbnail_bytes,
    get_active_alerts,
    get_device_telemetry,
    get_lake_health,
    get_lake_health_by_id,
    list_top_lakes,
    list_wishlist,
)

load_dotenv()  # walks up from this file to the repo-root .env, same as test_ai_key.py

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://localhost:5173")

# python-telegram-bot defaults to a 5 s connect timeout, which is far too tight on
# links that throttle Telegram: api.telegram.org has been measured taking ~19 s to
# finish its TLS handshake from an Indian consumer connection, so the default aborts
# during bootstrap's get_me() and the bot never starts. Generous ceilings only cost
# time on a genuinely dead network; they do not slow down a healthy one.
TELEGRAM_CONNECT_TIMEOUT_S = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT_S", "60"))
TELEGRAM_READ_TIMEOUT_S = float(os.getenv("TELEGRAM_READ_TIMEOUT_S", "60"))
# Long polling holds the request open for `timeout` seconds, so its read ceiling must
# clear that plus the handshake.
TELEGRAM_POLL_TIMEOUT_S = float(os.getenv("TELEGRAM_POLL_TIMEOUT_S", "30"))
# Retry the bootstrap handshake instead of dying on the first slow connect.
TELEGRAM_BOOTSTRAP_RETRIES = int(os.getenv("TELEGRAM_BOOTSTRAP_RETRIES", "5"))

NEARBY_RADIUS_KM = 15.0
MAX_TOOL_ROUNDS = 3  # a chained question ("find lakes, then check the biggest one") needs >1
QUICK_LAKES_ON_LIST = 6  # /list and /saved show this many inline-keyboard buttons

SYSTEM_PROMPT = (
    "You are JalNetra AI, a water quality & contamination intelligence assistant for "
    "Maharashtra and India, working from two kinds of evidence. You help officials "
    "monitor surface water bodies, lakes, and reservoirs.\n\n"
    "1. IN-SITU IoT SENSOR BUOY (primary for 'right now'): a deployed device at the "
    "Khadakwasla pilot site measuring water temperature, TDS (ppm), and turbidity "
    "(NTU) directly in the water. These are DIRECT MEASUREMENTS. For any question "
    "about current, live, measured or real-time conditions -- or anything naming the "
    "device, buoy or sensor -- call get_device_telemetry, and "
    "analyze_device_telemetry when asked what the readings mean or whether the water "
    "is safe.\n"
    "2. SENTINEL-2 SATELLITE indicators: Turbidity (NDTI), Chlorophyll-a (NDCI), "
    "Floating Algae (FAI), Surface Extent (MNDWI). These are PROXIES inferred from "
    "reflectance, covering the whole water body and years of history.\n\n"
    "Be precise about which source a number came from, and give the units. The buoy "
    "measures one point; the satellite sees the whole surface -- so when they "
    "disagree, that is informative (a local plume, a sensor drifting, a stale pass), "
    "not a contradiction to hide or average away. Never present a satellite proxy as "
    "a laboratory measurement. If the buoy is offline, say so plainly and fall back "
    "to satellite indicators rather than implying the device reported something. "
    "Always use the tools rather than guessing -- the platform has live device, "
    "registry, indicator and alert data. Explain limnology politely and "
    "scientifically."
)

WELCOME_MESSAGE = (
    "🌊 Welcome to JalNetra Water Intelligence Bot! 🛰️\n\n"
    "I monitor water quality from two sources — a *live IoT sensor buoy* in the "
    "water, and Sentinel-2 satellite imagery from orbit.\n\n"
    "📡 */device* — live readings from the buoy right now: temperature, dissolved "
    "solids (TDS), turbidity, probe diagnostics and signal strength\n"
    "🔬 */analyze* — an expert reading of what those numbers mean, with recommended "
    "actions\n"
    "🛰 */status <lake>* — satellite indicator card for any monitored lake\n"
    "📍 */nearby* — share your location to discover water bodies around you\n\n"
    "Or just ask in plain language — \"what's the water temperature now?\", \"is the "
    "water safe?\", \"is the sensor online?\" — and I'll read the device for you."
)

HELP_MESSAGE = (
    "*JalNetra Water Intelligence Bot — commands*\n\n"
    "/start — welcome message and what this bot can do\n"
    "/help — this list\n\n"
    "*Live IoT sensor buoy*\n"
    "/device — the buoy's current reading: temperature, TDS, turbidity, probe "
    "diagnostics and signal (/iot works too)\n"
    "/analyze — an expert limnologist's reading of those numbers, with recommended "
    "actions and a 7-14 day outlook\n\n"
    "*Satellite monitoring*\n"
    "/status <lake> — a satellite snapshot + indicator card for one lake\n"
    "/list — tap a monitored lake for its status card\n"
    "/saved — your wishlist, same tap-for-status buttons\n"
    "/nearby — share your location to find water bodies around you\n"
    "/alerts — currently active high-priority alerts\n"
    "/lakes — top monitored lakes across Maharashtra\n\n"
    "Otherwise, just type your question in plain language — e.g. \"What does high "
    "NDCI mean in a lake?\", \"Check water quality of Khadakwasla Reservoir\", or "
    "\"Scan Bhatghar Reservoir\" — and I'll answer directly, no command needed."
)

_openai_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set (checked the environment and .env).")
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


async def reply_markdown_safe(message: Message, text: str) -> None:
    """Lake, zone and district names routinely carry underscores/parentheses,
    which legacy Markdown reads as unmatched entity markers and rejects
    outright -- fall back to plain text rather than losing the reply."""
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await message.reply_text(text)


def _fmt_indicator(indicators: dict[str, Any], key: str) -> tuple[str, str]:
    r = indicators.get(key) or {}
    v = r.get("value")
    value_str = f"{v:.2f}" if isinstance(v, int | float) else "—"
    return value_str, r.get("status", "—")


def format_status_caption(health: dict[str, Any]) -> str:
    """The rich card attached to a lake's satellite photo -- also used
    text-only when no image is available (GEE off, or no pass that day)."""
    ind = health.get("indicators", {})
    ndti_v, ndti_s = _fmt_indicator(ind, "turbidity")
    ndci_v, ndci_s = _fmt_indicator(ind, "chlorophyll")
    extent = health.get("water_extent_km2")
    extent_str = f"{extent:.2f} km²" if isinstance(extent, int | float) else "—"
    clear = health.get("cloud_clear_pct")
    date_str = health.get("observed_on") or "no scene yet"
    if isinstance(clear, int | float):
        date_str += f" ({clear:.0f}% cloud-free)"
    return (
        f"🛰️ *Sentinel-2 Observation: {health.get('name', 'Unknown lake')}*\n"
        f"📅 *Date:* {date_str}\n"
        f"📏 *Water Extent:* {extent_str}\n"
        f"🌊 *Turbidity (NDTI):* {ndti_v} ({ndti_s})\n"
        f"🌿 *Chlorophyll (NDCI):* {ndci_v} ({ndci_s})\n"
        f"⚠️ *Active Alerts:* {health.get('open_alerts', 0)}\n"
        f"🔗 *Dashboard:* {DASHBOARD_BASE_URL}/?wb={health.get('water_body_id', '')}"
    )


async def send_lake_status(bot: Bot, chat_id: int, health: dict[str, Any]) -> None:
    """The photo + data card for one lake -- used by /status, the /list and
    /saved inline buttons, and natural-language lake-health questions alike."""
    caption = format_status_caption(health)
    photo = None
    observed_on = health.get("observed_on")
    wb_id = health.get("water_body_id")
    if observed_on and wb_id:
        photo = await fetch_thumbnail_bytes(wb_id, observed_on)
    if photo:
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode=ParseMode.MARKDOWN)
            return
        except BadRequest:
            log.warning("send_photo rejected the caption; falling back to text", extra={"chat_id": chat_id})
    try:
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=caption)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await reply_markdown_safe(update.message, HELP_MESSAGE)


async def nearby_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="📍 Share My Location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"Tap the button below to share your location — I'll find water bodies "
        f"within {NEARBY_RADIUS_KM:.0f} km.",
        reply_markup=keyboard,
    )


def _turbidity_reading(ntu: float, unit: str | None = "NTU") -> str:
    """A plain-language band for an NTU reading.

    Anchored on the WHO/BIS drinking-water guideline (< 1 NTU ideal, <= 5 NTU the
    acceptable limit) and ordinary surface-water practice above that. This describes
    clarity only -- turbidity is not a contamination verdict, and nothing here is a
    substitute for a lab assay.

    Returns "" for any other unit. The pilot ESP32 reports turbidity as a percentage,
    and there is no calibration here to turn that into NTU -- scoring a percentage
    against NTU thresholds would read as a guideline breach that was never measured.
    """
    if (unit or "NTU").strip().upper() not in ("NTU", ""):
        return ""
    if ntu < 1:
        return "very clear"
    if ntu < 5:
        return "clear"
    if ntu < 50:
        return "slightly turbid"
    if ntu < 200:
        return "turbid"
    return "very turbid"


def _tds_reading(ppm: float) -> str:
    """A plain-language band for TDS in ppm, on the usual potability scale."""
    if ppm < 50:
        return "very low mineral content"
    if ppm < 300:
        return "fresh, low mineralisation"
    if ppm < 600:
        return "good — typical freshwater"
    if ppm < 900:
        return "fair — noticeably mineralised"
    if ppm < 1200:
        return "poor — high dissolved solids"
    return "very high — brackish"


def _signal_bar(rssi: float) -> str:
    """WiFi RSSI in dBm as a coarse strength word, for field diagnostics."""
    if rssi >= -60:
        return "strong"
    if rssi >= -70:
        return "good"
    if rssi >= -80:
        return "weak"
    return "very weak"


def _fmt(value: Any, unit: str = "", nd: int = 2) -> str:
    """Numbers to a fixed precision, anything unreported as an em dash.

    A missing field means the firmware did not send it; showing a bare "0" there
    would invent a measurement the device never took.
    """
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        text = f"{value:.{nd}f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
        return f"{text} {unit}".strip()
    return f"{value} {unit}".strip()


def format_device_card(t: dict[str, Any]) -> str:
    """The buoy's full current payload as one Telegram card."""
    if not t.get("online"):
        return (
            "📡 *IoT Sensor Buoy — OFFLINE*\n\n"
            f"{t.get('error') or 'The device is not reachable right now.'}\n\n"
            "This is a normal field state: the buoy may be powered down, out of "
            "hotspot range, or mid-reboot. Satellite indicators are unaffected — try "
            "/status <lake> meanwhile."
        )

    lines = [
        "📡 *IoT Sensor Buoy — LIVE*",
        f"Device: `{t.get('device', 'unknown')}`",
        "",
        "*In-water measurements*",
    ]

    temp = t.get("temperature")
    lines.append(f"🌡 Temperature: *{_fmt(temp, t.get('temperature_unit', '°C'))}*")

    tds = t.get("tds")
    tds_line = f"🧪 TDS: *{_fmt(tds, t.get('tds_unit', 'ppm'))}*"
    if isinstance(tds, (int, float)):
        tds_line += f"\n     _{_tds_reading(float(tds))}_"
    lines.append(tds_line)

    ntu = t.get("turbidity")
    turb_unit = t.get("turbidity_unit", "NTU")
    turb_line = f"💧 Turbidity: *{_fmt(ntu, turb_unit)}*"
    if isinstance(ntu, (int, float)):
        band = _turbidity_reading(float(ntu), turb_unit)
        if band:
            turb_line += f"\n     _{band}_"
    lines.append(turb_line)

    # The device's own verdict, when its firmware offers one. Kept separate from the
    # probe-health field of the same name in the JSON contract.
    if t.get("device_status"):
        lines.append(f"📟 Device status: *{t['device_status']}*")

    # Raw ADC/voltage is what tells a field engineer a probe is drifting or
    # disconnected, so it belongs on the card rather than only in the API payload.
    raw: list[str] = []
    if t.get("tds_adc") is not None or t.get("tds_voltage") is not None:
        raw.append(
            f"TDS probe: ADC {_fmt(t.get('tds_adc'), nd=0)} · "
            f"{_fmt(t.get('tds_voltage'), 'V', 3)}"
        )
    if t.get("turbidity_adc") is not None or t.get("turbidity_voltage") is not None:
        raw.append(
            f"Turbidity probe: ADC {_fmt(t.get('turbidity_adc'), nd=0)} · "
            f"{_fmt(t.get('turbidity_voltage'), 'V', 3)}"
        )
    if t.get("turbidity_status"):
        raw.append(f"Probe status: {t['turbidity_status']}")
    if raw:
        lines += ["", "*Sensor diagnostics*"] + [f"• {r}" for r in raw]

    link: list[str] = []
    if t.get("wifi_rssi") is not None:
        rssi = t["wifi_rssi"]
        strength = f" ({_signal_bar(float(rssi))})" if isinstance(rssi, (int, float)) else ""
        link.append(f"Signal: {_fmt(rssi, 'dBm', 0)}{strength}")
    if t.get("wifi_ip"):
        link.append(f"IP: `{t['wifi_ip']}`")
    if t.get("reported_at"):
        link.append(f"Reported: {t['reported_at']}")
    elif t.get("timestamp_ms"):
        link.append(f"Device clock: {t['timestamp_ms']} ms since boot")
    if link:
        lines += ["", "*Link*"] + [f"• {item}" for item in link]

    lines += [
        "",
        "_One point measurement from a single buoy — indicative, not a lab assay._",
        "Tap /analyze for an expert reading of these numbers.",
    ]
    return "\n".join(lines)


def format_device_analysis(result: dict[str, Any]) -> str:
    """The AI limnologist's reading of the buoy sample."""
    analysis = result.get("analysis") or {}
    lines = [
        f"🔬 *Expert reading — {result.get('water_body', 'sensor buoy')}*",
        "",
        analysis.get("condition_summary", "No summary returned."),
    ]
    params = analysis.get("parameters") or {}
    labels = {
        "temperature_analysis": "🌡 Temperature",
        "tds_analysis": "🧪 Dissolved solids",
        "turbidity_analysis": "💧 Turbidity",
    }
    detail = [f"*{labels[k]}*\n{params[k]}" for k in labels if params.get(k)]
    if detail:
        lines += ["", *detail]
    if analysis.get("suggestions"):
        lines += ["", "*Recommended actions*"]
        lines += [f"{i}. {s}" for i, s in enumerate(analysis["suggestions"], 1)]
    if analysis.get("future_prediction"):
        lines += ["", f"*7-14 day outlook*\n{analysis['future_prediction']}"]
    lines += [
        "",
        "_An AI interpretation of one in-situ sample — not a scored JalNetra finding, "
        "and not a substitute for ground validation._",
    ]
    return "\n".join(lines)


async def device_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/device — the sensor buoy's current reading, every field it reported."""
    if not update.message:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    telemetry = await get_device_telemetry()
    keyboard = None
    if telemetry.get("online"):
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔬 Expert reading", callback_data="iot_analyze")],
                [InlineKeyboardButton("🔄 Refresh", callback_data="iot_refresh")],
            ]
        )
    try:
        await update.message.reply_text(
            format_device_card(telemetry), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    except BadRequest:
        await update.message.reply_text(format_device_card(telemetry), reply_markup=keyboard)


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/analyze — an AI limnologist's reading of the buoy's current sample."""
    if not update.message:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    result = await analyze_device_telemetry()
    if not result.get("online"):
        await reply_markdown_safe(update.message, format_device_card(result))
        return
    if result.get("error"):
        await update.message.reply_text(
            f"The buoy is reporting, but the AI reading failed: {result['error']}\n\n"
            "Use /device for the raw numbers."
        )
        return
    await reply_markdown_safe(update.message, format_device_analysis(result))


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    result = await get_active_alerts()
    if "error" in result:
        await update.message.reply_text(f"Could not load alerts: {result['error']}")
        return
    alerts = result.get("alerts", [])
    if not alerts:
        await update.message.reply_text(
            "✅ No active alerts right now — every monitored water body is within its "
            "normal baseline."
        )
        return
    lines = [f"🚨 *{result['total_open']} open alert(s)* — showing top {len(alerts)}:\n"]
    for a in alerts:
        trigger = a.get("trigger") or a["severity"].upper()
        lines.append(
            f"• *{a['water_body']}* ({a['zone']}) — {trigger}\n"
            f"  Severity: {a['severity'].upper()} · Priority {a['priority_score']:.0f}/100 · "
            f"{a['observed_on']}"
        )
    await reply_markdown_safe(update.message, "\n".join(lines))


async def lakes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    top = await list_top_lakes(10)
    if not top:
        await update.message.reply_text("No water bodies are registered yet.")
        return
    lines = ["🛰️ *Top monitored water bodies:*\n"]
    for i, wb in enumerate(top, 1):
        flag = " 🚨" if wb["open_alerts"] else ""
        lines.append(f"{i}. *{wb['name']}* — {wb['district']} · {wb['area_km2']:.1f} km²{flag}")
    await reply_markdown_safe(update.message, "\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Usage: /status <lake name> — e.g. /status Khadakwasla Reservoir")
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    health = await get_lake_health(name)
    if "error" in health:
        await update.message.reply_text(health["error"])
        return
    await send_lake_status(context.bot, update.effective_chat.id, health)


def _lake_buttons(lakes: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Two per row, callback_data carries the id so a tap goes straight to
    get_lake_health_by_id -- no re-typing, no name-matching round trip."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(lakes), 2):
        pair = lakes[i : i + 2]
        rows.append([InlineKeyboardButton(wb["name"], callback_data=f"status:{wb['id']}") for wb in pair])
    rows.append(
        [
            InlineKeyboardButton("🔍 Search other", callback_data="search_hint"),
            InlineKeyboardButton("⭐ View wishlist", callback_data="wishlist"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    top = await list_top_lakes(QUICK_LAKES_ON_LIST)
    if not top:
        await update.message.reply_text("No water bodies are registered yet.")
        return
    await update.message.reply_text(
        "🛰️ Monitored water bodies — tap one for its status:", reply_markup=_lake_buttons(top)
    )


async def saved_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    items = await list_wishlist()
    if not items:
        await update.message.reply_text(
            "Your wishlist is empty. Save a lake from the dashboard, or use /list to browse."
        )
        return
    lakes = [
        {"id": it["water_body_id"], "name": it.get("custom_name") or it["water_body"]["name"]}
        for it in items[:QUICK_LAKES_ON_LIST]
    ]
    await update.message.reply_text("⭐ Your saved lakes — tap one for its status:", reply_markup=_lake_buttons(lakes))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
    if data.startswith("status:"):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        health = await get_lake_health_by_id(data.removeprefix("status:"))
        if "error" in health:
            await context.bot.send_message(chat_id=chat_id, text=health["error"])
            return
        await send_lake_status(context.bot, chat_id, health)
    elif data in ("iot_refresh", "iot_analyze"):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        if data == "iot_refresh":
            text = format_device_card(await get_device_telemetry())
        else:
            result = await analyze_device_telemetry()
            if not result.get("online"):
                text = format_device_card(result)
            elif result.get("error"):
                text = (
                    f"The buoy is reporting, but the AI reading failed: {result['error']}\n\n"
                    "Use /device for the raw numbers."
                )
            else:
                text = format_device_analysis(result)
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=text)
    elif data == "search_hint":
        await context.bot.send_message(
            chat_id=chat_id, text="Type a lake name (e.g. \"Ambazari Lake\"), or use /nearby to search by location."
        )
    elif data == "wishlist":
        items = await list_wishlist()
        if not items:
            await context.bot.send_message(chat_id=chat_id, text="Your wishlist is empty.")
            return
        lakes = [
            {"id": it["water_body_id"], "name": it.get("custom_name") or it["water_body"]["name"]}
            for it in items[:QUICK_LAKES_ON_LIST]
        ]
        await context.bot.send_message(
            chat_id=chat_id, text="⭐ Your saved lakes:", reply_markup=_lake_buttons(lakes)
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.location is None:
        return
    lat, lon = message.location.latitude, message.location.longitude
    await message.reply_text(
        "🔍 Scanning satellite and mapping database for water bodies within "
        f"{NEARBY_RADIUS_KM:.0f} km of your location..."
    )
    result = await discover_lakes(lat, lon, NEARBY_RADIUS_KM)
    lakes = result.get("lakes", [])
    if not lakes:
        await message.reply_text(
            f"No significant water bodies found within {NEARBY_RADIUS_KM:.0f} km of this location."
        )
        return
    msg = f"📍 *Found {len(lakes)} Water Bodies near your location:*\n\n"
    for i, lake in enumerate(lakes, 1):
        area = lake.get("area_km2")
        area_str = f"{area:.2f}" if isinstance(area, int | float) else "N/A"
        msg += f"{i}. *{lake['name']}* — `{area_str} km²`\n"
    msg += f"\n💡 *Tip:* Ask me: 'Check water quality of {lakes[0]['name']}' to inspect it!"
    await reply_markdown_safe(message, msg)


async def _run_tool_call(name: str, raw_arguments: str) -> dict[str, Any]:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        args = json.loads(raw_arguments or "{}")
        result: Any = await fn(**args)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        log.exception("tool call failed", extra={"tool": name})
        return {"error": str(exc)}


async def ask_gpt(question: str) -> tuple[str, list[dict[str, Any]]]:
    """Ask GPT-4o-mini, letting it call JalNetra's own tools (Layer 2) for
    anything that needs live data -- a specific lake, nearby water bodies,
    open alerts, or a satellite scan -- rather than guessing. Each turn is
    still stateless (no cross-message memory), matching Layer 1's scope.

    Returns (reply_text, lake_cards): any successful get_lake_health /
    get_lake_health_by_id results along the way, so a natural-language "check
    on Khadakwasla" gets the same photo card /status sends, not just text."""
    client = _openai()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    lake_cards: list[dict[str, Any]] = []
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or "I didn't get a response — please try rephrasing.", lake_cards
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )
        for tc in msg.tool_calls:
            result = await _run_tool_call(tc.function.name, tc.function.arguments)
            if tc.function.name == "get_lake_health" and "error" not in result:
                lake_cards.append(result)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)}
            )
    return (
        "I looked into a few things but couldn't finish — please try asking again, more specifically.",
        lake_cards,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    await message.chat.send_action(ChatAction.TYPING)
    try:
        reply, lake_cards = await ask_gpt(message.text)
    except Exception:
        log.exception("gpt reply failed", extra={"chat_id": message.chat_id})
        await message.reply_text(
            "Sorry, I couldn't reach the AI service just now — please try again in a moment."
        )
        return
    # The photo card first (if the question resolved to a specific lake),
    # then GPT's own conversational answer.
    for card in lake_cards[:1]:
        await send_lake_status(context.bot, message.chat_id, card)
    await reply_markdown_safe(message, reply)


def build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (checked the environment and .env).")
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
        .read_timeout(TELEGRAM_READ_TIMEOUT_S)
        .write_timeout(TELEGRAM_READ_TIMEOUT_S)
        .pool_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
        .get_updates_connect_timeout(TELEGRAM_CONNECT_TIMEOUT_S)
        .get_updates_read_timeout(TELEGRAM_POLL_TIMEOUT_S + TELEGRAM_READ_TIMEOUT_S)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("device", device_command))
    application.add_handler(CommandHandler("iot", device_command))  # alias people reach for
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("saved", saved_command))
    application.add_handler(CommandHandler("nearby", nearby_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("lakes", lakes_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    application = build_application()
    log.info("JalNetra telegram bot starting (polling)")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=int(TELEGRAM_POLL_TIMEOUT_S),
        bootstrap_retries=TELEGRAM_BOOTSTRAP_RETRIES,
    )


if __name__ == "__main__":
    main()
