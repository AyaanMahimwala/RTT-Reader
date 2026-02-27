"""Google OAuth 2.0 flow for per-user calendar sync.

Handles:
- Generating OAuth authorization URLs with state-based user linking
- Exchanging authorization codes for credentials
- Persisting / loading / refreshing tokens per user
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# In-memory map: state_nonce → {user_id, chat_id, created_at}
# Entries expire after 10 minutes.
_oauth_pending: dict[str, dict] = {}
_PENDING_TTL = 600  # 10 minutes


def _get_client_config() -> dict:
    """Build the OAuth client config from environment variables."""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("OAUTH_REDIRECT_URI")
    if not all([client_id, client_secret, redirect_uri]):
        raise RuntimeError("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and OAUTH_REDIRECT_URI must be set")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def oauth_configured() -> bool:
    """Return True if the OAuth environment variables are set."""
    return bool(os.getenv("GOOGLE_CLIENT_ID"))


def create_auth_url(user_id: int, chat_id: int) -> str:
    """Generate a Google OAuth authorization URL and store pending state."""
    _cleanup_expired()
    nonce = secrets.token_urlsafe(32)
    state = f"{user_id}:{nonce}"

    config = _get_client_config()
    redirect_uri = os.getenv("OAUTH_REDIRECT_URI")
    flow = Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )

    _oauth_pending[nonce] = {
        "user_id": user_id,
        "chat_id": chat_id,
        "created_at": time.time(),
    }
    logger.info(f"Created OAuth URL for user {user_id}")
    return auth_url


def exchange_code(code: str, state: str) -> dict:
    """Exchange an authorization code for credentials.

    Returns: {"user_id": int, "chat_id": int, "credentials": Credentials}
    Raises ValueError if the state is invalid or expired.
    """
    _cleanup_expired()

    # state format: "{user_id}:{nonce}"
    parts = state.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Invalid OAuth state")

    user_id_str, nonce = parts
    pending = _oauth_pending.pop(nonce, None)
    if not pending:
        raise ValueError("OAuth link expired or already used. Please try /sync again.")

    if str(pending["user_id"]) != user_id_str:
        raise ValueError("OAuth state mismatch")

    config = _get_client_config()
    redirect_uri = os.getenv("OAUTH_REDIRECT_URI")
    flow = Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)
    flow.fetch_token(code=code)

    return {
        "user_id": pending["user_id"],
        "chat_id": pending["chat_id"],
        "credentials": flow.credentials,
    }


def get_token_path(data_dir: str) -> str:
    """Return the path to the user's stored Google OAuth token."""
    return os.path.join(data_dir, "google_token.json")


def save_credentials(credentials: Credentials, data_dir: str) -> None:
    """Persist OAuth credentials as JSON in the user's data directory."""
    token_path = get_token_path(data_dir)
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or []),
    }
    with open(token_path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved OAuth token to {token_path}")


def load_credentials(data_dir: str) -> Credentials | None:
    """Load and auto-refresh OAuth credentials from the user's data directory.

    Returns None if no token file exists or credentials are invalid.
    """
    token_path = get_token_path(data_dir)
    if not os.path.exists(token_path):
        return None

    with open(token_path) as f:
        data = json.load(f)

    creds = Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds, data_dir)
            logger.info("Refreshed expired OAuth token")
        except Exception:
            logger.exception("Failed to refresh OAuth token")
            return None

    return creds


def _cleanup_expired():
    """Remove expired entries from _oauth_pending."""
    now = time.time()
    expired = [k for k, v in _oauth_pending.items() if now - v["created_at"] > _PENDING_TTL]
    for k in expired:
        del _oauth_pending[k]
