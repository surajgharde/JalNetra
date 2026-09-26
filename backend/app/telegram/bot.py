"""JalNetra Telegram bot (Phase 2, Layer 1): a GPT-4o-mini assistant that
explains water-quality indicators and satellite monitoring. Location-based
lake discovery ("Layer 2") is not wired up yet -- /start already tells users
it's coming, so the promise and the code stay in sync.

Run it directly (polling, no webhook/ngrok needed):

    cd backend && uv run python -m app.telegram.bot
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
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

load_dotenv()  # walks up from this file to the repo-root .env, same as test_ai_key.py

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "You are JalNetra AI, an intelligent satellite-based water quality & "
    "contamination intelligence assistant for Maharashtra and India. You help "
    "officials monitor surface water bodies, lakes, and reservoirs. You explain "
    "indicators like Turbidity (NDTI), Chlorophyll-a (NDCI), Floating Algae (FAI), "
    "and Surface Extent (MNDWI) politely and scientifically."
)

WELCOME_MESSAGE = (
    "🌊 Welcome to JalNetra Water Intelligence Bot! 🛰️\n\n"
    "I am your AI assistant for monitoring lakes, reservoirs, and rivers across "
    "India using Sentinel-2 satellite imagery. You can:\n"
    "• Ask me questions about water quality and indicators.\n"
    "• Discuss contamination, turbidity, or algal blooms.\n"
    "• (Coming in Layer 2: Send your live location to discover surrounding lakes!)"
)

HELP_MESSAGE = (
    "*JalNetra Water Intelligence Bot — commands*\n\n"
    "/start — welcome message and what this bot can do\n"
    "/help — this list\n\n"
    "Otherwise, just type your question in plain language — e.g. \"What does high "
    "NDCI mean in a lake?\" or \"Can satellites detect chemical pollutants like "
    "arsenic?\" — and I'll answer directly, no command needed."
)

_openai_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set (checked the environment and .env).")
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.MARKDOWN)


async def ask_gpt(question: str) -> str:
    """One-shot completion: no cross-message memory yet, just this question
    against the system prompt -- matching the natural-language handler's
    current scope (Layer 1)."""
    response = await _openai().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
    )
    return response.choices[0].message.content or "I didn't get a response — please try rephrasing."


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
    try:
        # GPT's own markdown-ish output (asterisks, underscores) can trip
        # Telegram's strict entity parser; fall back to plain text rather
        # than silently dropping the reply.
        await message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await message.reply_text(reply)


def build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (checked the environment and .env).")
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    application = build_application()
    log.info("JalNetra telegram bot starting (polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
