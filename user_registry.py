"""Shared user registry functions used by telegram_bot.py and api.py."""

from __future__ import annotations

import json
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

ADMIN_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "0"))
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
USERS_FILE = os.path.join(_DATA_DIR, "users.json")


def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return {}


def save_users(users: dict) -> None:
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def get_user(user_id: int) -> dict | None:
    users = load_users()
    return users.get(str(user_id))


def get_user_data_dir(user_id: int) -> str:
    """Admin uses root DATA_DIR (backward compat), others get per-user dirs."""
    if user_id == ADMIN_USER_ID:
        return _DATA_DIR
    return os.path.join(_DATA_DIR, "users", str(user_id))


def ensure_admin_registered():
    """Auto-register admin user on startup with 'ready' status."""
    users = load_users()
    admin_key = str(ADMIN_USER_ID)
    if admin_key not in users:
        users[admin_key] = {
            "name": "Admin",
            "status": "ready",
            "registered_at": datetime.now().isoformat(),
            "is_admin": True,
        }
        save_users(users)
