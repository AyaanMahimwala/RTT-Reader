"""Admin OAuth + session logic for the internal dashboard.

Uses the same GOOGLE_CLIENT_ID/SECRET as user OAuth but with different scopes
(openid email profile instead of calendar) and a separate redirect URI.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

import httpx

from google_auth_oauthlib.flow import Flow
from user_registry import load_users, get_user_data_dir
from db import get_data_stats

logger = logging.getLogger(__name__)

ADMIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# In-memory map: nonce → {created_at}
_admin_oauth_pending: dict[str, dict] = {}
_PENDING_TTL = 600  # 10 minutes


def _get_admin_emails() -> list[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def _get_session_secret() -> str:
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("ADMIN_SESSION_SECRET must be set")
    return secret


def _cleanup_expired():
    now = time.time()
    expired = [k for k, v in _admin_oauth_pending.items() if now - v["created_at"] > _PENDING_TTL]
    for k in expired:
        del _admin_oauth_pending[k]


def create_admin_auth_url(redirect_uri: str) -> tuple[str, str]:
    """Build Google OAuth URL for admin login. Returns (auth_url, nonce)."""
    _cleanup_expired()

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not all([client_id, client_secret]):
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set")

    nonce = secrets.token_urlsafe(32)

    config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }

    flow = Flow.from_client_config(config, scopes=ADMIN_SCOPES, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="online",
        prompt="select_account",
        state=nonce,
    )

    _admin_oauth_pending[nonce] = {"created_at": time.time()}
    return auth_url, nonce


def exchange_admin_code(code: str, nonce: str, redirect_uri: str) -> str:
    """Exchange auth code, validate email against whitelist. Returns email.

    Raises ValueError if nonce invalid or email not whitelisted.
    """
    _cleanup_expired()

    pending = _admin_oauth_pending.pop(nonce, None)
    if not pending:
        raise ValueError("Login link expired or already used. Please try again.")

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }

    flow = Flow.from_client_config(config, scopes=ADMIN_SCOPES, redirect_uri=redirect_uri)
    flow.fetch_token(code=code)

    # Fetch user info
    token = flow.credentials.token
    resp = httpx.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    email = resp.json().get("email", "").lower()

    allowed = _get_admin_emails()
    if email not in allowed:
        raise ValueError(f"Access denied for {email}")

    return email


def create_session_cookie(email: str) -> str:
    """Create HMAC-signed session cookie: email:timestamp:signature."""
    secret = _get_session_secret()
    ts = str(int(time.time()))
    payload = f"{email}:{ts}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def validate_session_cookie(cookie: str) -> str | None:
    """Validate session cookie. Returns email if valid, None otherwise."""
    try:
        secret = _get_session_secret()
    except RuntimeError:
        return None

    parts = cookie.split(":")
    if len(parts) != 3:
        return None

    email, ts_str, sig = parts

    payload = f"{email}:{ts_str}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    # 24-hour expiry
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if time.time() - ts > 86400:
        return None

    return email


def get_all_users_stats() -> list[dict]:
    """Aggregate stats for all registered users."""
    users = load_users()
    results = []
    for uid, info in users.items():
        data_dir = get_user_data_dir(int(uid))
        stats = get_data_stats(data_dir)
        entry = {
            "user_id": uid,
            "name": info.get("name", "Unknown"),
            "status": info.get("status", "unknown"),
            "message_count": info.get("message_count", 0),
            "registered_at": info.get("registered_at", ""),
        }
        if stats:
            entry["event_count"] = stats["event_count"]
            entry["date_min"] = stats["date_min"]
            entry["date_max"] = stats["date_max"]
            entry["unique_people"] = stats["unique_people"]
        else:
            entry["event_count"] = 0
            entry["date_min"] = "-"
            entry["date_max"] = "-"
            entry["unique_people"] = 0
        results.append(entry)
    return results
