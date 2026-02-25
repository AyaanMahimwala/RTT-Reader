"""
Lightweight on-demand calendar sync.

Fetches only recent events from Google Calendar, diffs against the existing DB,
enriches new events, and upserts into SQLite + LanceDB.

Accepts an optional data_dir parameter for multi-user support.
"""

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))


def sync_calendar(data_dir=None) -> str:
    """Fetch recent events, enrich new ones, upsert into DB + vectors.

    Returns a human-readable status message.
    """
    d = data_dir or _DATA_DIR
    db_file = os.path.join(d, "calendar.db")
    csv_file = os.path.join(d, "calendar_raw_full.csv")
    taxonomy_file = os.path.join(d, "taxonomy.json")

    # Late imports to avoid circular deps and keep startup fast
    from data_extract import get_raw_calendar_data, export_to_csv  # noqa: delayed
    from etl import (
        load_cache, save_cache, parse_temporal_fields,
        run_pass1_discovery, run_pass2_enrichment,
        upsert_events, upsert_vectors,
    )
    from db import invalidate_vector_cache

    # 1. Determine sync start date — go back 7 days from today to catch gaps
    tz_name = os.getenv("YOUR_TIMEZONE", "America/Los_Angeles")
    today = datetime.now(ZoneInfo(tz_name)).date()
    sync_from = (today - timedelta(days=7)).isoformat()
    print(f"Syncing from {sync_from} (7 days back from {today})")

    # 2. Fetch recent events from Google Calendar
    raw_events = get_raw_calendar_data(sync_from)
    print(f"Fetched {len(raw_events)} events from Google Calendar since {sync_from}")

    # 3. Convert to CSV-row dicts and find truly new event_ids
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

    # 4. Append new events to the CSV (keep it as source of truth)
    import csv
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for ev in new_events:
            writer.writerow([
                ev["event_id"], ev["summary"], ev["start_dt"], ev["end_dt"],
                ev["description"], ev["location"], ev["status"],
            ])

    # 5. Run Pass 1 (discovery) and Pass 2 (enrichment) on new events only
    #    The caches will skip all previously-enriched events automatically.
    with open(taxonomy_file) as f:
        taxonomy = json.load(f)

    run_pass1_discovery(new_events, data_dir=data_dir)
    enrichment_cache = run_pass2_enrichment(new_events, taxonomy, data_dir=data_dir)

    # 6. Upsert into SQLite
    upsert_events(new_events, enrichment_cache, new_ids, data_dir=data_dir)

    # 7. Upsert into LanceDB
    upsert_vectors(new_events, enrichment_cache, new_ids, data_dir=data_dir)

    # 8. Invalidate the vector table cache so queries see the new data
    invalidate_vector_cache(data_dir)

    # 9. Build detailed stats
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


if __name__ == "__main__":
    print(sync_calendar())
