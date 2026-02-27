"""
FastAPI server — thin wrapper around the shared agent in agent.py.

Also hosts the /auth/callback endpoint for per-user Google Calendar OAuth.

Usage:
    uvicorn api:app --reload
    curl -X POST http://localhost:8000/query -H "Content-Type: application/json" \
         -d '{"question": "Who did I hang out with the most?"}'
"""

import asyncio
import logging
import threading
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent
from db import list_memories, delete_memory

logger = logging.getLogger(__name__)

app = FastAPI(title="Calendar Query API", version="2.0.0")

# Set by telegram_bot.py at startup so the OAuth callback can send Telegram messages.
_telegram_bot = None


def set_telegram_bot(bot):
    """Store a reference to the Telegram bot for sending post-OAuth messages."""
    global _telegram_bot
    _telegram_bot = bot


# ──────────────────────────────────────────────
# API Models & Endpoint
# ──────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    sql_queries: list[str]
    data: list[dict]
    session_id: str


@app.post("/query", response_model=QueryResponse)
async def query_calendar(request: QueryRequest):
    """Answer a natural language question about the calendar data."""
    result = run_agent(request.question, request.session_id)
    return QueryResponse(**result)


@app.get("/memories")
async def get_memories():
    """Return all saved memories."""
    return list_memories()


@app.delete("/memories/{memory_id}")
async def remove_memory(memory_id: str):
    """Delete a memory by id."""
    if delete_memory(memory_id):
        return {"status": "deleted"}
    return {"status": "not_found"}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ──────────────────────────────────────────────
# OAuth callback
# ──────────────────────────────────────────────

@app.get("/auth/callback")
async def oauth_callback(request: Request):
    """Handle Google OAuth redirect after user grants calendar access."""
    from google_auth import exchange_code, save_credentials
    from user_registry import get_user_data_dir, load_users, save_users

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(
            "<h2>Authorization denied</h2>"
            f"<p>Google returned an error: {error}</p>"
            "<p>Go back to Telegram and try /sync again.</p>",
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse(
            "<h2>Missing parameters</h2>"
            "<p>Go back to Telegram and try /sync again.</p>",
            status_code=400,
        )

    try:
        result = exchange_code(code, state)
    except ValueError as e:
        return HTMLResponse(
            f"<h2>Authorization failed</h2>"
            f"<p>{e}</p>"
            "<p>Go back to Telegram and try /sync again.</p>",
            status_code=400,
        )

    user_id = result["user_id"]
    chat_id = result["chat_id"]
    credentials = result["credentials"]
    data_dir = get_user_data_dir(user_id)

    save_credentials(credentials, data_dir)
    logger.info(f"OAuth token saved for user {user_id}")

    # Trigger sync in a background thread so we can return the HTML immediately
    def _background_sync():
        try:
            from sync import sync_calendar_oauth
            from agent import invalidate_schema_cache

            sync_result = sync_calendar_oauth(data_dir, credentials)
            invalidate_schema_cache(data_dir)

            # Update user status
            users = load_users()
            uid = str(user_id)
            if uid in users:
                users[uid]["status"] = "ready"
                save_users(users)

            # Send Telegram notification
            if _telegram_bot:
                msg = f"Google Calendar connected! {sync_result}"
                asyncio.run(_telegram_bot.send_message(chat_id=chat_id, text=msg))
        except Exception:
            logger.exception(f"Post-OAuth sync failed for user {user_id}")
            if _telegram_bot:
                try:
                    asyncio.run(_telegram_bot.send_message(
                        chat_id=chat_id,
                        text="Google Calendar connected, but the initial sync failed. Try /sync again.",
                    ))
                except Exception:
                    pass

    threading.Thread(target=_background_sync, daemon=True).start()

    return HTMLResponse(
        "<h2>Success!</h2>"
        "<p>Your Google Calendar is now connected.</p>"
        "<p>Go back to Telegram — I'm syncing your events now.</p>"
    )


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
