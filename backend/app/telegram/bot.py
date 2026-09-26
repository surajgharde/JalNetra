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
from telegram import KeyboardButton, Message, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.core.logging import configure_logging
from app.telegram.tools import TOOL_FUNCTIONS, TOOLS, discover_lakes, get_active_alerts, list_top_lakes

load_dotenv()  # walks up from this file to the repo-root .env, same as test_ai_key.py

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

NEARBY_RADIUS_KM = 15.0
MAX_TOOL_ROUNDS = 3  # a chained question ("find lakes, then check the biggest one") needs >1

SYSTEM_PROMPT = (
    "You are JalNetra AI, an intelligent satellite-based water quality & "
    "contamination intelligence assistant for Maharashtra and India. You help "
    "officials monitor surface water bodies, lakes, and reservoirs. You explain "
    "indicators like Turbidity (NDTI), Chlorophyll-a (NDCI), Floating Algae (FAI), "
    "and Surface Extent (MNDWI) politely and scientifically. When asked about a "
    "specific lake's current status, nearby water bodies, active alerts, or to run "
    "a satellite scan, use the tools available to you rather than guessing -- the "
    "platform has live registry, indicator and alert data."
)

WELCOME_MESSAGE = (
    "🌊 Welcome to JalNetra Water Intelligence Bot! 🛰️\n\n"
    "I am your AI assistant for monitoring lakes, reservoirs, and rivers across "
    "India using Sentinel-2 satellite imagery. You can:\n"
    "• Ask me questions about water quality and indicators.\n"
    "• Discuss contamination, turbidity, or algal blooms.\n"
    "• Send your live location (or /nearby) to discover surrounding lakes!"
)

HELP_MESSAGE = (
    "*JalNetra Water Intelligence Bot — commands*\n\n"
    "/start — welcome message and what this bot can do\n"
    "/help — this list\n"
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


async def ask_gpt(question: str) -> str:
    """Ask GPT-4o-mini, letting it call JalNetra's own tools (Layer 2) for
    anything that needs live data -- a specific lake, nearby water bodies,
    open alerts, or a satellite scan -- rather than guessing. Each turn is
    still stateless (no cross-message memory), matching Layer 1's scope."""
    client = _openai()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or "I didn't get a response — please try rephrasing."
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
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)}
            )
    return "I looked into a few things but couldn't finish — please try asking again, more specifically."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    await message.chat.send_action(ChatAction.TYPING)
    try:
        reply = await ask_gpt(message.text)
    except Exception:
        log.exception("gpt reply failed", extra={"chat_id": message.chat_id})
        await message.reply_text(
            "Sorry, I couldn't reach the AI service just now — please try again in a moment."
        )
        return
    await reply_markdown_safe(message, reply)


def build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (checked the environment and .env).")
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("nearby", nearby_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("lakes", lakes_command))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    application = build_application()
    log.info("JalNetra telegram bot starting (polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
