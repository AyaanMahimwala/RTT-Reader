"""
Telegram bot interface for the calendar analytics agent.

Uses long-polling (no public URL / webhooks needed).

Usage:
    python telegram_bot.py

Environment variables (.env):
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_USER_ID    — your numeric Telegram user ID (from @userinfobot)
"""

import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from agent import run_agent, reset_session

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Map Telegram chat_id → agent session_id
_chat_sessions: dict[int, str] = {}


def _is_authorized(update: Update) -> bool:
    return update.effective_user.id == TELEGRAM_USER_ID


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text at paragraph boundaries to fit Telegram's message limit."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find the last double newline within the limit
        split_at = text.rfind("\n\n", 0, limit)
        if split_at == -1:
            # Fall back to single newline
            split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            # Fall back to hard cut
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def _send_long(update: Update, text: str) -> None:
    """Send a message, splitting if needed. Try Markdown, fall back to plain text."""
    for chunk in _split_message(text):
        try:
            await update.message.reply_text(
                chunk, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            await update.message.reply_text(chunk)


# ──────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text(
        "Hey! I'm your calendar analytics bot.\n\n"
        "Ask me anything about your calendar data.\n\n"
        "Commands:\n"
        "/new — start a fresh conversation\n"
        "/sync — refresh calendar data from Google\n"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    chat_id = update.effective_chat.id
    old_sid = _chat_sessions.pop(chat_id, None)
    if old_sid:
        reset_session(old_sid)
    await update.message.reply_text("Session reset. Ask me anything!")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_chat_action(ChatAction.TYPING)
    try:
        from sync import sync_calendar
        result = sync_calendar()
        await update.message.reply_text(result)
    except Exception as e:
        logger.exception("Sync failed")
        await update.message.reply_text(f"Sync failed: {e}")


# ──────────────────────────────────────────────
# Message handler (agent queries)
# ──────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    chat_id = update.effective_chat.id
    question = update.message.text

    # Show "typing..." while processing
    await update.message.reply_chat_action(ChatAction.TYPING)

    session_id = _chat_sessions.get(chat_id)

    try:
        result = run_agent(question, session_id)
        _chat_sessions[chat_id] = result["session_id"]
        await _send_long(update, result["answer"])
    except Exception as e:
        logger.exception("Agent error")
        await update.message.reply_text(f"Error: {e}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        return
    if not TELEGRAM_USER_ID:
        print("Error: TELEGRAM_USER_ID not set in .env")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot starting in polling mode...")
    app.run_polling()


if __name__ == "__main__":
    main()
