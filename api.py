"""
FastAPI server — thin wrapper around the shared agent in agent.py.

Also hosts the /auth/callback endpoint for per-user Google Calendar OAuth.

In production (WEBHOOK_URL set), this is the primary process:
  - Uvicorn listens on PORT
  - Telegram bot runs in webhook mode (POST /telegram-webhook)
  - OAuth callbacks handled at /auth/callback

In local dev (no WEBHOOK_URL), telegram_bot.py runs polling mode and starts
this server in a daemon thread for OAuth callbacks only.

Usage:
    uvicorn api:app --reload                    # local dev (API only)
    python telegram_bot.py                      # local dev (polling + daemon web server)
    uvicorn api:app --host 0.0.0.0 --port 8000  # production (webhook mode)
"""

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent
from db import list_memories, delete_memory

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Set by telegram_bot.py at startup for local-dev polling mode only.
_telegram_bot = None


def set_telegram_bot(bot):
    """Store a reference to the Telegram bot for sending post-OAuth messages (local dev only)."""
    global _telegram_bot
    _telegram_bot = bot


def _get_bot(request: Request):
    """Get the Telegram bot instance — from app.state (webhook) or global (polling)."""
    bot_app = getattr(request.app.state, "bot_app", None)
    if bot_app:
        return bot_app.bot
    return _telegram_bot


# ──────────────────────────────────────────────
# Lifespan — webhook mode setup/teardown
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage Telegram webhook bot lifecycle when WEBHOOK_URL is set."""
    if WEBHOOK_URL:
        from user_registry import ensure_admin_registered
        from telegram_bot import create_webhook_application

        ensure_admin_registered()

        bot_app = create_webhook_application()
        await bot_app.initialize()
        await bot_app.bot.set_webhook(url=WEBHOOK_URL)
        await bot_app.start()
        app.state.bot_app = bot_app
        logger.info(f"Telegram webhook set to {WEBHOOK_URL}")

        yield

        await bot_app.stop()
        await bot_app.bot.delete_webhook()
        await bot_app.shutdown()
        logger.info("Telegram webhook removed")
    else:
        yield


app = FastAPI(title="Calendar Query API", version="2.0.0", lifespan=lifespan)


# ──────────────────────────────────────────────
# Telegram webhook endpoint
# ──────────────────────────────────────────────

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook (production mode)."""
    from telegram import Update

    bot_app = getattr(request.app.state, "bot_app", None)
    if not bot_app:
        return JSONResponse({"error": "Bot not running in webhook mode"}, status_code=500)

    data = await request.json()
    update = Update.de_json(data=data, bot=bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}


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

    bot = _get_bot(request)
    loop = asyncio.get_running_loop()

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
            if bot:
                msg = f"Google Calendar connected! {sync_result}"
                asyncio.run_coroutine_threadsafe(
                    bot.send_message(chat_id=chat_id, text=msg), loop
                ).result(timeout=30)
        except Exception:
            logger.exception(f"Post-OAuth sync failed for user {user_id}")
            if bot:
                try:
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(
                            chat_id=chat_id,
                            text="Google Calendar connected, but the initial sync failed. Try /sync again.",
                        ), loop
                    ).result(timeout=30)
                except Exception:
                    pass

    threading.Thread(target=_background_sync, daemon=True).start()

    return HTMLResponse(
        "<h2>Success!</h2>"
        "<p>Your Google Calendar is now connected.</p>"
        "<p>Go back to Telegram — I'm syncing your events now.</p>"
    )


# ──────────────────────────────────────────────
# Admin dashboard routes
# ──────────────────────────────────────────────

def _admin_redirect_uri() -> str:
    """Build the admin OAuth callback URI from OAUTH_REDIRECT_URI base."""
    base_uri = os.getenv("OAUTH_REDIRECT_URI", "")
    # Derive base URL from existing OAuth redirect URI (e.g. https://host/auth/callback → https://host)
    base = base_uri.rsplit("/", 2)[0] if "/" in base_uri else base_uri
    return f"{base}/admin/auth/callback"


@app.get("/admin/login")
async def admin_login(request: Request):
    from admin_auth import validate_session_cookie

    cookie = request.cookies.get("admin_session")
    if cookie and validate_session_cookie(cookie):
        return RedirectResponse(url="/admin")

    from admin_dashboard import render_login_page
    return HTMLResponse(render_login_page())


@app.get("/admin/login/start")
async def admin_login_start(request: Request):
    from admin_auth import create_admin_auth_url

    redirect_uri = _admin_redirect_uri()
    auth_url, _nonce = create_admin_auth_url(redirect_uri)
    return RedirectResponse(auth_url)


@app.get("/admin/auth/callback")
async def admin_auth_callback(request: Request):
    from admin_auth import exchange_admin_code, create_session_cookie
    from admin_dashboard import render_error_page

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(render_error_page(f"Google returned: {error}"), status_code=400)

    if not code or not state:
        return HTMLResponse(render_error_page("Missing parameters. Please try again."), status_code=400)

    redirect_uri = _admin_redirect_uri()

    try:
        email = exchange_admin_code(code, state, redirect_uri)
    except ValueError as e:
        return HTMLResponse(render_error_page(str(e)), status_code=403)

    cookie = create_session_cookie(email)
    response = RedirectResponse(url="/admin", status_code=302)
    is_https = _admin_redirect_uri().startswith("https")
    response.set_cookie(
        key="admin_session",
        value=cookie,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=86400,
    )
    return response


@app.get("/admin")
async def admin_dashboard(request: Request):
    from admin_auth import validate_session_cookie, get_all_users_stats
    from admin_dashboard import render_dashboard

    cookie = request.cookies.get("admin_session")
    if not cookie:
        return RedirectResponse(url="/admin/login")

    email = validate_session_cookie(cookie)
    if not email:
        return RedirectResponse(url="/admin/login")

    stats = get_all_users_stats()
    return HTMLResponse(render_dashboard(stats, email))


@app.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("admin_session")
    return response


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
