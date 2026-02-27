"""Shared user registry functions used by telegram_bot.py and api.py."""

from __future__ import annotations

import json
import os
import shutil
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
    """Return per-user data directory: data/users/{user_id}/."""
    return os.path.join(_DATA_DIR, "users", str(user_id))


def _migrate_admin_data():
    """One-time migration: move legacy root data files to data/users/<ADMIN_USER_ID>/."""
    user_dir = get_user_data_dir(ADMIN_USER_ID)
    print(f"[migration] Checking migration for admin {ADMIN_USER_ID}")
    print(f"[migration] Root data dir: {_DATA_DIR}")
    print(f"[migration] Target user dir: {user_dir}")

    # Only migrate if the user dir is empty/missing and root files exist
    if os.path.exists(user_dir) and os.listdir(user_dir):
        print(f"[migration] SKIP — user dir already exists with contents: {os.listdir(user_dir)}")
        return False

    files_to_move = [
        "calendar_raw_full.csv",
        "calendar.db",
        "calendar_events.db",
        "taxonomy.json",
        "discovery_cache.json",
        "enrichment_cache.json",
        "google_token.json",
    ]
    dirs_to_move = ["calendar_vectors"]

    # Log what exists at root
    for name in files_to_move + dirs_to_move:
        src = os.path.join(_DATA_DIR, name)
        exists = os.path.exists(src)
        print(f"[migration]   {name}: {'FOUND' if exists else 'not found'}")

    has_anything = any(
        os.path.exists(os.path.join(_DATA_DIR, f)) for f in files_to_move + dirs_to_move
    )
    if not has_anything:
        print("[migration] SKIP — no root files to migrate")
        return False

    os.makedirs(user_dir, exist_ok=True)
    migrated = []

    for name in files_to_move:
        src = os.path.join(_DATA_DIR, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(user_dir, name))
            migrated.append(name)

    for name in dirs_to_move:
        src = os.path.join(_DATA_DIR, name)
        if os.path.isdir(src):
            shutil.move(src, os.path.join(user_dir, name))
            migrated.append(name + "/")

    print(f"[migration] DONE — moved to {user_dir}: {', '.join(migrated)}")

    # Log final state of user dir
    print(f"[migration] User dir contents: {os.listdir(user_dir)}")

    return True


def ensure_admin_registered():
    """Auto-register admin user on startup (needs OAuth sync like everyone else)."""
    users = load_users()
    admin_key = str(ADMIN_USER_ID)
    print(f"[admin-reg] Admin key: {admin_key}, already registered: {admin_key in users}")

    # Migrate legacy root data files on first run
    migrated = _migrate_admin_data()

    if admin_key not in users:
        # Determine initial status: if migrated data includes a DB, mark ready
        user_dir = get_user_data_dir(ADMIN_USER_ID)
        has_db = os.path.exists(os.path.join(user_dir, "calendar_events.db")) or \
                 os.path.exists(os.path.join(user_dir, "calendar.db"))
        status = "ready" if has_db else "registered"
        print(f"[admin-reg] New registration — has_db={has_db}, status={status}")

        users[admin_key] = {
            "name": "Admin",
            "status": status,
            "registered_at": datetime.now().isoformat(),
        }
        save_users(users)
    elif migrated:
        # Already registered but just migrated data — update status if DB exists
        user_dir = get_user_data_dir(ADMIN_USER_ID)
        has_db = os.path.exists(os.path.join(user_dir, "calendar_events.db")) or \
                 os.path.exists(os.path.join(user_dir, "calendar.db"))
        print(f"[admin-reg] Already registered + migrated — has_db={has_db}, current status={users[admin_key].get('status')}")
        if has_db and users[admin_key].get("status") != "ready":
            users[admin_key]["status"] = "ready"
            save_users(users)
    else:
        print(f"[admin-reg] Already registered, no migration — status={users[admin_key].get('status')}")
