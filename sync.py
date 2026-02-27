"""
Lightweight on-demand calendar sync via OAuth.

Fetches only recent events from Google Calendar, diffs against the existing DB,
enriches new events, and upserts into SQLite + LanceDB.

All users authenticate via OAuth (sync_calendar_oauth).
"""

import csv
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))


# ──────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────

def _raw_to_dicts(raw_events: list[dict]) -> list[dict]:
    """Convert Google Calendar API event objects to flat CSV-row dicts."""
    fetched = []
    for event in raw_events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        fetched.append({
            "event_id": event.get("id"),
            "summary": event.get("summary", ""),
            "start_dt": start,
            "end_dt": end,
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "status": event.get("status", ""),
        })
    return fetched


def _sync_events(raw_events: list[dict], data_dir: str) -> str:
    """Diff, enrich, and upsert new events into an existing DB.

    Expects the DB and taxonomy to already exist at data_dir.
    Returns a human-readable status message.
    """
    from etl import (
        parse_temporal_fields,
        run_pass1_discovery, run_pass2_enrichment,
        upsert_events, upsert_vectors,
    )
    from db import invalidate_vector_cache

    db_file = os.path.join(data_dir, "calendar.db")
    csv_file = os.path.join(data_dir, "calendar_raw_full.csv")
    taxonomy_file = os.path.join(data_dir, "taxonomy.json")

    fetched = _raw_to_dicts(raw_events)

    # Check which event_ids already exist in the DB
    conn = sqlite3.connect(db_file)
    existing = set()
    cursor = conn.execute("SELECT event_id FROM events")
    for r in cursor:
        existing.add(r[0])
    conn.close()

    new_ids = set()
    new_events = []
    for ev in fetched:
        if ev["event_id"] not in existing:
            new_ids.add(ev["event_id"])
            new_events.append(ev)

    if not new_ids:
        return "Database is up to date — no new events found."

    print(f"Found {len(new_ids)} new events to sync")

    # Append new events to the CSV
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for ev in new_events:
            writer.writerow([
                ev["event_id"], ev["summary"], ev["start_dt"], ev["end_dt"],
                ev["description"], ev["location"], ev["status"],
            ])

    # Enrich and upsert
    with open(taxonomy_file) as f:
        taxonomy = json.load(f)

    run_pass1_discovery(new_events, data_dir=data_dir)
    enrichment_cache = run_pass2_enrichment(new_events, taxonomy, data_dir=data_dir)
    upsert_events(new_events, enrichment_cache, new_ids, data_dir=data_dir)
    upsert_vectors(new_events, enrichment_cache, new_ids, data_dir=data_dir)
    invalidate_vector_cache(data_dir)

    # Stats
    date_counts = Counter()
    for ev in new_events:
        fields = parse_temporal_fields(ev)
        date_counts[fields["date"]] += 1

    lines = [f"Synced {len(new_ids)} new events into the database.\n"]
    lines.append("New events by date:")
    for date in sorted(date_counts):
        lines.append(f"  {date}: {date_counts[date]} events")
    date_range = sorted(date_counts)
    lines.append(f"\nDate range: {date_range[0]} to {date_range[-1]}")
    return "\n".join(lines)


def _first_time_sync(raw_events: list[dict], data_dir: str) -> str:
    """Bootstrap a user's data directory from scratch using fetched events.

    Writes CSV, runs full ETL, and returns a status message.
    """
    from data_extract import export_to_csv
    from etl import run_etl

    os.makedirs(data_dir, exist_ok=True)
    csv_file = os.path.join(data_dir, "calendar_raw_full.csv")
    export_to_csv(raw_events, csv_file)
    print(f"Wrote {len(raw_events)} events to {csv_file}")

    result = run_etl(data_dir)
    return f"Initial sync complete — {len(raw_events)} events imported.\n\n{result}"


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def sync_calendar_oauth(data_dir: str, credentials, calendar_id: str = "primary") -> str:
    """OAuth-based calendar sync.

    If the user has no DB yet, does a full first-time sync (fetches all events).
    Otherwise, incremental sync (last 7 days).

    Returns a human-readable status message.
    """
    from data_extract import get_raw_calendar_data_with_creds

    db_file = os.path.join(data_dir, "calendar.db")
    is_first_time = not os.path.exists(db_file)

    tz_name = os.getenv("YOUR_TIMEZONE", "America/Los_Angeles")
    today = datetime.now(ZoneInfo(tz_name)).date()

    if is_first_time:
        # First-time: fetch from 1 year ago
        sync_from = (today - timedelta(days=365)).isoformat()
    else:
        sync_from = (today - timedelta(days=7)).isoformat()

    print(f"OAuth sync from {sync_from} (first_time={is_first_time})")
    raw_events = get_raw_calendar_data_with_creds(sync_from, credentials, calendar_id)
    print(f"Fetched {len(raw_events)} events via OAuth")

    if not raw_events:
        return "No events found in your Google Calendar."

    if is_first_time:
        return _first_time_sync(raw_events, data_dir)
    else:
        return _sync_events(raw_events, data_dir)
