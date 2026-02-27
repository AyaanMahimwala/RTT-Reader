"""
Multi-user Telegram bot interface for the calendar analytics agent.

Uses long-polling (no public URL / webhooks needed).

Users:
  - /register to create an account
  - Upload a .ics or .zip calendar export from Google Calendar
  - /process to run the ETL pipeline on their data
  - Then ask questions about their calendar

Admin user (TELEGRAM_USER_ID) is auto-registered with existing data.

Usage:
    python telegram_bot.py

Environment variables (.env):
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_USER_ID    — admin's numeric Telegram user ID (from @userinfobot)
"""

import asyncio
import os
import logging
from datetime import datetime

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

from agent import run_agent, reset_session, invalidate_schema_cache
from user_registry import (
    ADMIN_USER_ID, load_users, save_users, get_user, get_user_data_dir,
    ensure_admin_registered,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Map Telegram chat_id → agent session_id
_chat_sessions: dict[int, str] = {}


# ──────────────────────────────────────────────
# Message splitting
# ──────────────────────────────────────────────

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
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Welcome to the Calendar Analytics Bot!\n\n"
            "Send /register to get started."
        )
        return

    status = user.get("status", "registered")
    if status == "ready":
        await update.message.reply_text(
            "Hey! I'm your calendar analytics bot.\n\n"
            "Ask me anything about your calendar data.\n\n"
            "Commands:\n"
            "/new — start a fresh conversation\n"
            "/status — check your data status\n"
            "/sync — sync calendar data from Google\n"
        )
    else:
        await update.message.reply_text(
            "Welcome back! Your account status: " + status + "\n\n"
            "Commands:\n"
            "/status — check what to do next\n"
        )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_id_str = str(user_id)
    users = load_users()

    if user_id_str in users:
        await update.message.reply_text(
            "You're already registered! Send /status to see what's next."
        )
        return

    # Create user entry + data directory
    data_dir = get_user_data_dir(user_id)
    os.makedirs(data_dir, exist_ok=True)

    users[user_id_str] = {
        "name": update.effective_user.first_name or "User",
        "status": "registered",
        "registered_at": datetime.now().isoformat(),
        "is_admin": False,
    }
    save_users(users)

    # Auto-trigger OAuth flow if configured
    from google_auth import oauth_configured, create_auth_url

    if oauth_configured():
        try:
            chat_id = update.effective_chat.id
            auth_url = create_auth_url(user_id, chat_id)
            await update.message.reply_text(
                "You're registered! Let's connect your Google Calendar.\n\n"
                f"Click this link to authorize:\n{auth_url}\n\n"
                "Once you approve, I'll automatically sync your calendar data."
            )
        except Exception as e:
            logger.exception("Failed to create OAuth URL during registration")
            await update.message.reply_text(
                "You're registered! Send /sync to connect your Google Calendar."
            )
    else:
        await update.message.reply_text(
            "You're registered! Send /sync to connect your Google Calendar."
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("You're not registered yet. Send /register to get started.")
        return

    status = user.get("status", "registered")
    name = user.get("name", "User")

    status_messages = {
        "registered": (
            f"Hi {name}! You're registered but haven't connected your calendar yet.\n\n"
            "Next step: Send /sync to connect your Google Calendar."
        ),
        "data_uploaded": (
            f"Hi {name}! Your calendar data is uploaded "
            f"({user.get('event_count', '?')} events found).\n\n"
            "Next step: Send /process to analyze your data."
        ),
        "processing": (
            f"Hi {name}! Your data is currently being processed. "
            "This can take several minutes — I'll let you know when it's done."
        ),
        "ready": (
            f"Hi {name}! Your data is ready. Ask me anything about your calendar!\n\n"
            "Use /sync to update your calendar data."
        ),
        "error": (
            f"Hi {name}! There was an error processing your data.\n"
            f"Error: {user.get('error', 'Unknown')}\n\n"
            "Try /sync to reconnect your calendar, or contact support."
        ),
    }
    msg = status_messages.get(status, f"Status: {status}")

    # Show Google Calendar connection status
    from google_auth import oauth_configured, get_token_path
    data_dir = get_user_data_dir(update.effective_user.id)
    if user.get("is_admin") and os.getenv("SERVICE_ACCOUNT_FILE"):
        msg += "\n\nGoogle Calendar: connected (service account)"
    elif oauth_configured():
        token_path = get_token_path(data_dir)
        if os.path.exists(token_path):
            msg += "\n\nGoogle Calendar: connected (OAuth)"
        else:
            msg += "\n\nGoogle Calendar: not connected — send /sync to link your account"

    await update.message.reply_text(msg)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id)
    if not user:
        return
    chat_id = update.effective_chat.id
    old_sid = _chat_sessions.pop(chat_id, None)
    if old_sid:
        reset_session(old_sid)
    await update.message.reply_text("Session reset. Ask me anything!")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sync calendar data from Google — admin uses service account, others use OAuth."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Please /register first!")
        return

    data_dir = get_user_data_dir(user_id)

    # Admin path: use service account (backward compatible)
    if user.get("is_admin") and os.getenv("SERVICE_ACCOUNT_FILE"):
        await update.message.reply_chat_action(ChatAction.TYPING)
        try:
            from sync import sync_calendar
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, sync_calendar)
            invalidate_schema_cache(data_dir)
            await update.message.reply_text(result)
        except Exception as e:
            logger.exception("Sync failed")
            await update.message.reply_text(f"Sync failed: {e}")
        return

    # OAuth path: check if we have a stored token
    from google_auth import load_credentials, oauth_configured, create_auth_url

    if not oauth_configured():
        await update.message.reply_text(
            "Google Calendar sync isn't configured yet.\n\n"
            "You can still upload a .ics or .zip calendar export manually."
        )
        return

    creds = load_credentials(data_dir)
    if creds:
        # Already authorized — sync directly
        await update.message.reply_chat_action(ChatAction.TYPING)
        await update.message.reply_text("Syncing your calendar...")
        try:
            from sync import sync_calendar_oauth
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: sync_calendar_oauth(data_dir, creds)
            )
            invalidate_schema_cache(data_dir)

            users = load_users()
            users[str(user_id)]["status"] = "ready"
            save_users(users)

            await update.message.reply_text(result)
        except Exception as e:
            logger.exception("OAuth sync failed")
            await update.message.reply_text(f"Sync failed: {e}")
    else:
        # No token yet — send OAuth link
        try:
            chat_id = update.effective_chat.id
            auth_url = create_auth_url(user_id, chat_id)
            await update.message.reply_text(
                "To sync your Google Calendar, I need to connect to your Google account.\n\n"
                f"Click this link to authorize:\n{auth_url}\n\n"
                "The link expires in 10 minutes."
            )
        except Exception as e:
            logger.exception("Failed to create OAuth URL")
            await update.message.reply_text(f"Error: {e}")


async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run ETL pipeline on user's uploaded calendar data."""
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user:
        await update.message.reply_text("Please /register first!")
        return

    data_dir = get_user_data_dir(user_id)
    csv_path = os.path.join(data_dir, "calendar_raw_full.csv")

    if not os.path.exists(csv_path):
        await update.message.reply_text(
            "No calendar data found. Please upload your .ics or .zip file first."
        )
        return

    if user.get("status") == "processing":
        await update.message.reply_text("Already processing your data. Please wait...")
        return

    # Update status
    users = load_users()
    users[str(user_id)]["status"] = "processing"
    save_users(users)

    await update.message.reply_text(
        "Starting data processing... This may take several minutes.\n"
        "I'll message you when it's done."
    )

    try:
        from etl import run_etl

        # Run ETL in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: run_etl(data_dir))

        # Invalidate caches so next query sees the new data
        invalidate_schema_cache(data_dir)

        users = load_users()
        users[str(user_id)]["status"] = "ready"
        save_users(users)

        await _send_long(update, f"{result}\n\nAsk me anything about your calendar!")

    except Exception as e:
        users = load_users()
        users[str(user_id)]["status"] = "error"
        users[str(user_id)]["error"] = str(e)
        save_users(users)

        logger.exception("ETL failed")
        await update.message.reply_text(f"Processing failed: {e}")


# ──────────────────────────────────────────────
# File upload handler (ICS/ZIP calendar exports)
# ──────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle uploaded .ics or .zip calendar export files."""
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user:
        await update.message.reply_text("Please /register first!")
        return

    doc = update.message.document
    filename = doc.file_name or "upload"

    if not (filename.lower().endswith(".ics") or filename.lower().endswith(".zip")):
        await update.message.reply_text(
            "Please send a .ics or .zip calendar export file.\n\n"
            "To export from Google Calendar:\n"
            "1. Go to calendar.google.com\n"
            "2. Settings (gear icon) → Import & Export → Export"
        )
        return

    await update.message.reply_chat_action(ChatAction.TYPING)

    # Download file from Telegram
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Error downloading file: {e}")
        return

    # Parse the calendar file
    try:
        from ics_parser import parse_upload, events_to_csv

        events = parse_upload(bytes(file_bytes), filename)
    except Exception as e:
        logger.exception("ICS parse failed")
        await update.message.reply_text(f"Error parsing calendar file: {e}")
        return

    if not events:
        await update.message.reply_text("No calendar events found in the file.")
        return

    # Save as CSV in user's data directory
    data_dir = get_user_data_dir(user_id)
    os.makedirs(data_dir, exist_ok=True)
    csv_path = os.path.join(data_dir, "calendar_raw_full.csv")
    events_to_csv(events, csv_path)

    # Update user status
    users = load_users()
    users[str(user_id)]["status"] = "data_uploaded"
    users[str(user_id)]["event_count"] = len(events)
    save_users(users)

    await update.message.reply_text(
        f"Calendar data received! {len(events):,} events found.\n\n"
        f"Send /process to start analyzing your data.\n"
        f"(This will take a few minutes)"
    )


# ──────────────────────────────────────────────
# Message handler (agent queries)
# ──────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user:
        await update.message.reply_text(
            "Please /register first to use this bot."
        )
        return

    status = user.get("status", "registered")
    if status != "ready":
        status_hints = {
            "registered": "Please connect your Google Calendar first — send /sync.",
            "data_uploaded": "Your data is uploaded. Send /process to analyze it.",
            "processing": "Your data is still being processed. Please wait...",
            "error": "There was an error processing your data. Try /sync to reconnect your calendar, or send /process to retry.",
        }
        await update.message.reply_text(
            status_hints.get(status, "Something went wrong. Try /status for details.")
        )
        return

    chat_id = update.effective_chat.id
    question = update.message.text
    data_dir = get_user_data_dir(user_id)

    # Show "typing..." while processing
    await update.message.reply_chat_action(ChatAction.TYPING)

    session_id = _chat_sessions.get(chat_id)

    try:
        result = run_agent(question, session_id, data_dir=data_dir)
        _chat_sessions[chat_id] = result["session_id"]
        await _send_long(update, result["answer"])
    except Exception as e:
        logger.exception("Agent error")
        await update.message.reply_text(f"Error: {e}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def _start_web_server():
    """Start uvicorn in a daemon thread for the OAuth callback endpoint."""
    import threading
    import uvicorn
    from api import app as web_app

    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Starting OAuth callback server on port {port}")
    threading.Thread(
        target=uvicorn.run,
        args=(web_app,),
        kwargs={"host": "0.0.0.0", "port": port, "log_level": "warning"},
        daemon=True,
    ).start()


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        return
    if not ADMIN_USER_ID:
        print("Error: TELEGRAM_USER_ID not set in .env")
        return

    # Auto-register admin user
    ensure_admin_registered()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Start the web server for OAuth callbacks (only if OAuth is configured)
    from google_auth import oauth_configured
    if oauth_configured():
        from api import set_telegram_bot
        set_telegram_bot(app.bot)
        _start_web_server()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot starting in polling mode (multi-user)...")
    app.run_polling()


if __name__ == "__main__":
    main()
