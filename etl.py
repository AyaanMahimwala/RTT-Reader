"""
ETL Pipeline: calendar_raw_full.csv → LLM-enriched SQLite database + LanceDB vector store

Two-pass enrichment:
  Pass 1: Category discovery — LLM freely tags events, then consolidates into taxonomy
  Pass 2: Full structured extraction using discovered taxonomy
  Pass 3: Embed sub-activities into LanceDB for semantic search
"""

import csv
import json
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import lancedb
from anthropic import Anthropic
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YOUR_TIMEZONE = os.getenv("YOUR_TIMEZONE", "America/Chicago")

CSV_FILE = "calendar_raw_full.csv"
DB_FILE = "calendar.db"
DISCOVERY_CACHE = "discovery_cache.json"
TAXONOMY_FILE = "taxonomy.json"
ENRICHMENT_CACHE = "enrichment_cache.json"
VECTOR_DIR = "calendar_vectors"
EMBEDDING_MODEL = "text-embedding-3-small"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

client = Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ──────────────────────────────────────────────
# CSV Reading + Basic Parsing
# ──────────────────────────────────────────────

def read_csv():
    """Read calendar_raw_full.csv and return list of dicts."""
    events = []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            events.append(row)
    print(f"Read {len(events)} events from {CSV_FILE}")
    return events


def parse_temporal_fields(event):
    """Extract date, year, month, day_of_week, start_hour, duration_minutes from start/end datetimes."""
    tz = ZoneInfo(YOUR_TIMEZONE)
    start_str = event["start_dt"]
    end_str = event["end_dt"]

    # Handle all-day events (date only, no 'T')
    if "T" not in start_str:
        dt_start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=tz)
        dt_end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        dt_start = datetime.fromisoformat(start_str).astimezone(tz)
        dt_end = datetime.fromisoformat(end_str).astimezone(tz)

    duration = (dt_end - dt_start).total_seconds() / 60.0

    return {
        "date": dt_start.strftime("%Y-%m-%d"),
        "year": dt_start.year,
        "month": dt_start.month,
        "day_of_week": dt_start.strftime("%A"),
        "start_hour": round(dt_start.hour + dt_start.minute / 60.0, 2),
        "duration_minutes": round(duration, 1),
    }


# ──────────────────────────────────────────────
# Pass 1: Category Discovery
# ──────────────────────────────────────────────

def load_cache(path):
    """Load a JSON cache file, or return empty dict."""
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_cache(path, data):
    """Atomically save cache to disk."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def run_discovery_batch(batch):
    """Send a batch of events to Haiku for free-form tagging.
    Returns dict: {event_id: [tag1, tag2, ...], ...}
    """
    lines = []
    for i, ev in enumerate(batch, 1):
        lines.append(f'{i}. [{ev["event_id"]}] "{ev["summary"]}"')
    events_block = "\n".join(lines)

    prompt = f"""For each calendar event summary below, provide 1-3 short activity type tags that describe the activity.
Be specific (e.g., "deep_work" not just "work", "bar_hopping" not just "social", "cooking" not just "food").
Use snake_case for all tags. Keep tags short (1-3 words max).

Respond ONLY with valid JSON mapping event_id to a list of tags:
{{"event_id": ["tag1", "tag2"], ...}}

Events:
{events_block}"""

    for attempt in range(3):
        try:
            response = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            # Try to extract JSON from the response
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
        except (json.JSONDecodeError, Exception) as e:
            if attempt < 2:
                print(f"    Retry {attempt+1} for discovery batch: {e}")
                time.sleep(1)
            else:
                print(f"    FAILED discovery batch after 3 attempts: {e}")
                # Return empty tags for this batch
                return {ev["event_id"]: [] for ev in batch}


def run_pass1_discovery(events):
    """Pass 1: Discover categories by tagging all events."""
    cache = load_cache(DISCOVERY_CACHE)
    batch_size = 50
    total_batches = (len(events) + batch_size - 1) // batch_size

    print(f"\n{'='*60}")
    print(f"PASS 1: Category Discovery ({total_batches} batches of {batch_size})")
    print(f"{'='*60}")

    processed = 0
    for i in range(0, len(events), batch_size):
        batch = events[i : i + batch_size]
        batch_num = i // batch_size + 1

        # Skip if all events in this batch are already cached
        uncached = [ev for ev in batch if ev["event_id"] not in cache]
        if not uncached:
            processed += len(batch)
            continue

        print(f"  Batch {batch_num}/{total_batches} ({len(uncached)} new events)...", end=" ", flush=True)
        result = run_discovery_batch(uncached)
        cache.update(result)
        save_cache(DISCOVERY_CACHE, cache)
        processed += len(batch)
        print(f"done ({processed}/{len(events)})")

        # Rate limit: ~50 req/min for Haiku
        time.sleep(0.3)

    print(f"\nDiscovery complete: {len(cache)} events tagged")
    return cache


def consolidate_taxonomy(discovery_tags):
    """Take all raw tags and ask Claude to create a clean taxonomy."""
    # Count tag frequencies
    tag_counts = {}
    for tags in discovery_tags.values():
        for tag in tags:
            tag = tag.lower().strip()
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Sort by frequency
    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    top_tags = sorted_tags[:200]  # Send top 200 tags

    tags_text = "\n".join(f"  {tag}: {count}" for tag, count in top_tags)

    prompt = f"""Here are all activity tags found across 8,224 calendar events from a personal time-tracking calendar, with frequencies:

{tags_text}

Group these into a clean taxonomy of 10-20 categories. Each category should be broad enough to be useful for querying but specific enough to be meaningful.

Important guidelines:
- Include a category for wasted/unproductive time (phone scrolling, social media, etc.)
- Distinguish between different types of work (deep work, meetings, side projects)
- Have separate categories for sleep, commute/transit, and personal care/routine
- Include social, family, exercise, food, entertainment, travel, errands, personal growth

Return ONLY valid JSON in this format:
{{
  "categories": [
    {{
      "name": "category_name",
      "description": "What this category covers",
      "raw_tags": ["tag1", "tag2", "tag3"]
    }}
  ]
}}"""

    print("\nConsolidating taxonomy...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    taxonomy = json.loads(text)
    save_cache(TAXONOMY_FILE, taxonomy)
    print(f"Taxonomy saved: {len(taxonomy['categories'])} categories")
    for cat in taxonomy["categories"]:
        print(f"  - {cat['name']}: {cat['description']} ({len(cat['raw_tags'])} tags)")
    return taxonomy


# ──────────────────────────────────────────────
# Pass 2: Full Structured Enrichment
# ──────────────────────────────────────────────

def build_enrichment_prompt(taxonomy):
    """Build the system context for enrichment, including the taxonomy."""
    cat_descriptions = []
    for cat in taxonomy["categories"]:
        tags_str = ", ".join(cat["raw_tags"][:10])
        cat_descriptions.append(f'  - {cat["name"]}: {cat["description"]} (tags: {tags_str})')
    categories_block = "\n".join(cat_descriptions)
    category_names = [cat["name"] for cat in taxonomy["categories"]]

    return f"""You are enriching personal calendar events with structured metadata.

CATEGORY TAXONOMY (use ONLY these category names):
{categories_block}

Valid category names: {json.dumps(category_names)}

For each event, extract:
1. sub_activities: Break compound events into individual activities. If the event has commas or multiple activities mentioned, decompose. For simple/atomic events, use a single-item list with the activity.
2. people: Names of specific people mentioned. Use their first name only. Exclude generic references like "friends" or "coworkers" unless a specific name is given.
3. locations: Specific places mentioned (neighborhoods, restaurants, venues, addresses). Not generic ("home" is ok if explicitly stated).
4. categories: 1-3 categories from the taxonomy above that best describe this event.
5. work_depth: "deep", "medium", "shallow", or "meeting" if this is a work event. null otherwise.
6. mood: "positive", "negative", or "neutral" based on the emotional tone. null if no clear signal.
7. is_productive: true if the time was spent intentionally on something valuable (work, exercise, cooking, learning). false for passive consumption or wasted time.
8. is_wasted_time: true if the event clearly represents wasted/unproductive time (social media, phone scrolling, oversleeping, "rot", taking an L).

Respond ONLY with valid JSON — an array of objects in the same order as the events given."""


def run_enrichment_batch(batch, system_context):
    """Send a batch of events to Haiku for full enrichment."""
    lines = []
    for i, ev in enumerate(batch, 1):
        lines.append(
            f'{i}. event_id="{ev["event_id"]}" | summary="{ev["summary"]}" | '
            f'start="{ev["start_dt"]}" | end="{ev["end_dt"]}" | '
            f'description="{ev.get("description", "")}" | location="{ev.get("location", "")}"'
        )
    events_block = "\n".join(lines)

    prompt = f"""Enrich these calendar events. Return a JSON array of objects, one per event, in order.

Each object must have these fields:
- "event_id": string (copy from input)
- "sub_activities": list of strings
- "people": list of strings
- "locations": list of strings
- "categories": list of strings (from taxonomy)
- "work_depth": string or null
- "mood": string or null
- "is_productive": boolean
- "is_wasted_time": boolean

Events:
{events_block}"""

    for attempt in range(3):
        try:
            response = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=8192,
                system=system_context,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            results = json.loads(text)
            # Validate it's a list
            if not isinstance(results, list):
                raise ValueError("Expected JSON array")
            return results
        except (json.JSONDecodeError, ValueError, Exception) as e:
            if attempt < 2:
                print(f"    Retry {attempt+1} for enrichment batch: {e}")
                time.sleep(1)
            else:
                print(f"    FAILED enrichment batch after 3 attempts: {e}")
                # Return minimal enrichment
                return [
                    {
                        "event_id": ev["event_id"],
                        "sub_activities": [ev["summary"]],
                        "people": [],
                        "locations": [],
                        "categories": [],
                        "work_depth": None,
                        "mood": None,
                        "is_productive": False,
                        "is_wasted_time": False,
                    }
                    for ev in batch
                ]


def run_pass2_enrichment(events, taxonomy):
    """Pass 2: Full structured enrichment using the discovered taxonomy."""
    cache = load_cache(ENRICHMENT_CACHE)
    system_context = build_enrichment_prompt(taxonomy)
    batch_size = 20
    total_batches = (len(events) + batch_size - 1) // batch_size

    print(f"\n{'='*60}")
    print(f"PASS 2: Full Enrichment ({total_batches} batches of {batch_size})")
    print(f"{'='*60}")

    processed = 0
    for i in range(0, len(events), batch_size):
        batch = events[i : i + batch_size]
        batch_num = i // batch_size + 1

        # Skip if all events in this batch are already cached
        uncached = [ev for ev in batch if ev["event_id"] not in cache]
        if not uncached:
            processed += len(batch)
            continue

        print(f"  Batch {batch_num}/{total_batches} ({len(uncached)} new events)...", end=" ", flush=True)
        results = run_enrichment_batch(uncached, system_context)

        # Map results back by event_id
        for result in results:
            eid = result.get("event_id")
            if eid:
                cache[eid] = result

        save_cache(ENRICHMENT_CACHE, cache)
        processed += len(batch)
        print(f"done ({processed}/{len(events)})")

        time.sleep(0.3)

    print(f"\nEnrichment complete: {len(cache)} events enriched")
    return cache


# ──────────────────────────────────────────────
# SQLite Loading
# ──────────────────────────────────────────────

def create_database(events, enrichment_cache):
    """Create SQLite database from events + enrichment data."""
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Create tables
    c.execute("""
        CREATE TABLE events (
            event_id         TEXT PRIMARY KEY,
            summary          TEXT,
            start_dt         TEXT,
            end_dt           TEXT,
            date             TEXT,
            year             INTEGER,
            month            INTEGER,
            day_of_week      TEXT,
            start_hour       REAL,
            duration_minutes REAL,
            categories       TEXT,
            people           TEXT,
            locations        TEXT,
            work_depth       TEXT,
            mood             TEXT,
            is_productive    BOOLEAN,
            is_wasted_time   BOOLEAN,
            description      TEXT,
            location_raw     TEXT
        )
    """)

    c.execute("""
        CREATE TABLE sub_activities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT,
            activity    TEXT,
            category    TEXT,
            people      TEXT,
            FOREIGN KEY (event_id) REFERENCES events(event_id)
        )
    """)

    c.execute("""
        CREATE TABLE event_people (
            event_id TEXT,
            person   TEXT,
            FOREIGN KEY (event_id) REFERENCES events(event_id)
        )
    """)

    print(f"\nLoading {len(events)} events into {DB_FILE}...")

    for event in events:
        eid = event["event_id"]
        temporal = parse_temporal_fields(event)
        enrichment = enrichment_cache.get(eid, {})

        categories = ",".join(enrichment.get("categories", []))
        people = ",".join(enrichment.get("people", []))
        locations = ",".join(enrichment.get("locations", []))
        work_depth = enrichment.get("work_depth")
        mood = enrichment.get("mood")
        is_productive = enrichment.get("is_productive", False)
        is_wasted_time = enrichment.get("is_wasted_time", False)

        c.execute(
            """INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                eid,
                event["summary"],
                event["start_dt"],
                event["end_dt"],
                temporal["date"],
                temporal["year"],
                temporal["month"],
                temporal["day_of_week"],
                temporal["start_hour"],
                temporal["duration_minutes"],
                categories,
                people,
                locations,
                work_depth,
                mood,
                is_productive,
                is_wasted_time,
                event.get("description", ""),
                event.get("location", ""),
            ),
        )

        # Insert sub_activities
        sub_activities = enrichment.get("sub_activities", [event["summary"]])
        enrichment_categories = enrichment.get("categories", [])
        primary_category = enrichment_categories[0] if enrichment_categories else None

        for activity in sub_activities:
            c.execute(
                "INSERT INTO sub_activities (event_id, activity, category, people) VALUES (?,?,?,?)",
                (eid, activity, primary_category, people),
            )

        # Insert event_people (normalized)
        for person in enrichment.get("people", []):
            c.execute(
                "INSERT INTO event_people (event_id, person) VALUES (?,?)",
                (eid, person),
            )

    # Create indexes
    c.execute("CREATE INDEX idx_events_date ON events(date)")
    c.execute("CREATE INDEX idx_events_year_month ON events(year, month)")
    c.execute("CREATE INDEX idx_events_categories ON events(categories)")
    c.execute("CREATE INDEX idx_event_people_person ON event_people(person)")
    c.execute("CREATE INDEX idx_sub_activities_event ON sub_activities(event_id)")
    c.execute("CREATE INDEX idx_sub_activities_category ON sub_activities(category)")

    conn.commit()

    # Print stats
    c.execute("SELECT COUNT(*) FROM events")
    event_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM sub_activities")
    sub_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM event_people")
    people_count = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT person) FROM event_people")
    unique_people = c.fetchone()[0]

    print(f"\nDatabase created:")
    print(f"  Events:         {event_count}")
    print(f"  Sub-activities:  {sub_count}")
    print(f"  People links:    {people_count} ({unique_people} unique people)")

    conn.close()


def upsert_events(events, enrichment_cache, event_ids):
    """Insert new events into existing SQLite database (no drop/recreate).

    Only processes events whose event_id is in event_ids.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    inserted = 0
    for event in events:
        eid = event["event_id"]
        if eid not in event_ids:
            continue

        temporal = parse_temporal_fields(event)
        enrichment = enrichment_cache.get(eid, {})

        categories = ",".join(enrichment.get("categories", []))
        people = ",".join(enrichment.get("people", []))
        locations = ",".join(enrichment.get("locations", []))
        work_depth = enrichment.get("work_depth")
        mood = enrichment.get("mood")
        is_productive = enrichment.get("is_productive", False)
        is_wasted_time = enrichment.get("is_wasted_time", False)

        c.execute(
            """INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                eid,
                event["summary"],
                event["start_dt"],
                event["end_dt"],
                temporal["date"],
                temporal["year"],
                temporal["month"],
                temporal["day_of_week"],
                temporal["start_hour"],
                temporal["duration_minutes"],
                categories,
                people,
                locations,
                work_depth,
                mood,
                is_productive,
                is_wasted_time,
                event.get("description", ""),
                event.get("location", ""),
            ),
        )

        # Insert sub_activities
        sub_activities = enrichment.get("sub_activities", [event["summary"]])
        enrichment_categories = enrichment.get("categories", [])
        primary_category = enrichment_categories[0] if enrichment_categories else None

        for activity in sub_activities:
            c.execute(
                "INSERT INTO sub_activities (event_id, activity, category, people) VALUES (?,?,?,?)",
                (eid, activity, primary_category, people),
            )

        # Insert event_people (normalized)
        for person in enrichment.get("people", []):
            c.execute(
                "INSERT INTO event_people (event_id, person) VALUES (?,?)",
                (eid, person),
            )

        inserted += 1

    conn.commit()
    conn.close()
    print(f"Upserted {inserted} events into {DB_FILE}")
    return inserted


# ──────────────────────────────────────────────
# Vector Store (LanceDB)
# ──────────────────────────────────────────────

def _time_band(start_hour):
    """Convert decimal hour to semantic time band."""
    if start_hour < 6:
        return "early morning"
    elif start_hour < 12:
        return "morning"
    elif start_hour < 17:
        return "afternoon"
    elif start_hour < 21:
        return "evening"
    else:
        return "late night"


def construct_embedding_text(activity, activity_index, all_sub_activities, event, enrichment, temporal):
    """Build selective prose text for a sub-activity embedding.

    Includes: activity, location, category, sibling context (before/after),
    time band, weekday/weekend, people, mood/productivity.
    Excludes: exact date, exact hour, duration (metadata filters instead).
    """
    parts = []

    # Activity name + location
    locations = enrichment.get("locations", [])
    if locations:
        parts.append(f"{activity} at {', '.join(locations)}")
    else:
        parts.append(activity)

    # Category
    categories = enrichment.get("categories", [])
    if categories:
        cat_str = "/".join(c.replace("_", " ") for c in categories)
        parts.append(f"({cat_str})")

    # Sibling context (before/after, not flat list)
    if len(all_sub_activities) > 1:
        before = all_sub_activities[activity_index - 1] if activity_index > 0 else None
        after = all_sub_activities[activity_index + 1] if activity_index < len(all_sub_activities) - 1 else None
        context_parts = []
        if before:
            context_parts.append(f"after {before}")
        if after:
            context_parts.append(f"before {after}")
        if context_parts:
            parts.append(". " + ", ".join(context_parts).capitalize())

    # Time band + weekday/weekend
    day_of_week = temporal["day_of_week"]
    is_weekend = day_of_week in ("Saturday", "Sunday")
    time_str = f"{'Weekend' if is_weekend else 'Weekday'} {_time_band(temporal['start_hour'])}"
    parts.append(f". {time_str}")

    # People
    people = enrichment.get("people", [])
    if people:
        parts.append(f", with {', '.join(people)}")
    else:
        parts.append(", solo")

    # Mood + productivity
    mood = enrichment.get("mood")
    is_productive = enrichment.get("is_productive", False)
    is_wasted = enrichment.get("is_wasted_time", False)
    mood_parts = []
    if mood:
        mood_parts.append(f"mood: {mood}")
    if is_wasted:
        mood_parts.append("unproductive")
    elif is_productive:
        mood_parts.append("productive")
    if mood_parts:
        parts.append(f". {', '.join(mood_parts).capitalize()}.")

    return "".join(parts)


def embed_batch(texts):
    """Embed a batch of texts using OpenAI's embedding API."""
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
    )
    return [item.embedding for item in response.data]


def create_vector_store(events, enrichment_cache):
    """Create LanceDB vector store from enriched sub-activities."""
    print(f"\n{'='*60}")
    print("PASS 3: Vector Store Creation (LanceDB)")
    print(f"{'='*60}")

    # Build all records first
    records = []
    for event in events:
        eid = event["event_id"]
        enrichment = enrichment_cache.get(eid, {})
        temporal = parse_temporal_fields(event)
        sub_activities = enrichment.get("sub_activities", [event["summary"]])
        categories = enrichment.get("categories", [])
        primary_category = categories[0] if categories else ""

        for idx, activity in enumerate(sub_activities):
            text = construct_embedding_text(
                activity, idx, sub_activities, event, enrichment, temporal
            )
            records.append({
                "text": text,
                "event_id": eid,
                "activity": activity,
                "parent_summary": event["summary"],
                "category": primary_category,
                "categories": ",".join(categories),
                "date": temporal["date"],
                "year": temporal["year"],
                "month": temporal["month"],
                "day_of_week": temporal["day_of_week"],
                "start_hour": temporal["start_hour"],
                "duration_minutes": temporal["duration_minutes"],
                "people": ",".join(enrichment.get("people", [])),
                "locations": ",".join(enrichment.get("locations", [])),
                "mood": enrichment.get("mood") or "",
                "work_depth": enrichment.get("work_depth") or "",
                "is_productive": bool(enrichment.get("is_productive", False)),
                "is_wasted_time": bool(enrichment.get("is_wasted_time", False)),
            })

    print(f"  Built {len(records)} sub-activity records")

    # Embed in batches of 100
    batch_size = 100
    all_vectors = []
    total_batches = (len(records) + batch_size - 1) // batch_size

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        batch_num = i // batch_size + 1
        texts = [r["text"] for r in batch]

        print(f"  Embedding batch {batch_num}/{total_batches}...", end=" ", flush=True)
        vectors = embed_batch(texts)
        all_vectors.extend(vectors)
        print("done")

        # Respect rate limits
        if batch_num < total_batches:
            time.sleep(0.1)

    # Attach vectors to records
    for record, vector in zip(records, all_vectors):
        record["vector"] = vector

    # Create LanceDB table
    db = lancedb.connect(VECTOR_DIR)

    # Drop existing table if present
    existing_tables = db.table_names()
    if "sub_activities" in existing_tables:
        db.drop_table("sub_activities")

    table = db.create_table("sub_activities", records)

    print(f"\n  Vector store created: {VECTOR_DIR}/")
    print(f"  Table: sub_activities ({len(records)} rows, {len(all_vectors[0])} dimensions)")


def upsert_vectors(events, enrichment_cache, event_ids):
    """Embed and append new sub-activities to existing LanceDB table.

    Only processes events whose event_id is in event_ids.
    """
    # Build records for new events only
    records = []
    for event in events:
        eid = event["event_id"]
        if eid not in event_ids:
            continue

        enrichment = enrichment_cache.get(eid, {})
        temporal = parse_temporal_fields(event)
        sub_activities = enrichment.get("sub_activities", [event["summary"]])
        categories = enrichment.get("categories", [])
        primary_category = categories[0] if categories else ""

        for idx, activity in enumerate(sub_activities):
            text = construct_embedding_text(
                activity, idx, sub_activities, event, enrichment, temporal
            )
            records.append({
                "text": text,
                "event_id": eid,
                "activity": activity,
                "parent_summary": event["summary"],
                "category": primary_category,
                "categories": ",".join(categories),
                "date": temporal["date"],
                "year": temporal["year"],
                "month": temporal["month"],
                "day_of_week": temporal["day_of_week"],
                "start_hour": temporal["start_hour"],
                "duration_minutes": temporal["duration_minutes"],
                "people": ",".join(enrichment.get("people", [])),
                "locations": ",".join(enrichment.get("locations", [])),
                "mood": enrichment.get("mood") or "",
                "work_depth": enrichment.get("work_depth") or "",
                "is_productive": bool(enrichment.get("is_productive", False)),
                "is_wasted_time": bool(enrichment.get("is_wasted_time", False)),
            })

    if not records:
        print("No new sub-activities to embed.")
        return 0

    print(f"Embedding {len(records)} new sub-activity records...")

    # Embed in batches of 100
    batch_size = 100
    all_vectors = []
    total_batches = (len(records) + batch_size - 1) // batch_size

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        batch_num = i // batch_size + 1
        texts = [r["text"] for r in batch]

        print(f"  Embedding batch {batch_num}/{total_batches}...", end=" ", flush=True)
        vectors = embed_batch(texts)
        all_vectors.extend(vectors)
        print("done")

        if batch_num < total_batches:
            time.sleep(0.1)

    # Attach vectors to records
    for record, vector in zip(records, all_vectors):
        record["vector"] = vector

    # Append to existing LanceDB table
    db = lancedb.connect(VECTOR_DIR)
    table = db.open_table("sub_activities")
    table.add(records)

    print(f"Added {len(records)} vectors to LanceDB")
    return len(records)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    start_time = time.time()

    # 1. Read CSV
    events = read_csv()

    # 2. Pass 1: Discovery
    discovery_tags = run_pass1_discovery(events)

    # 3. Consolidate taxonomy
    if os.path.exists(TAXONOMY_FILE):
        print(f"\nUsing existing taxonomy from {TAXONOMY_FILE}")
        with open(TAXONOMY_FILE) as f:
            taxonomy = json.load(f)
        for cat in taxonomy["categories"]:
            print(f"  - {cat['name']}: {cat['description']}")
    else:
        taxonomy = consolidate_taxonomy(discovery_tags)

    # 4. Pass 2: Enrichment
    enrichment_cache = run_pass2_enrichment(events, taxonomy)

    # 5. Load into SQLite
    create_database(events, enrichment_cache)

    # 6. Build vector store
    create_vector_store(events, enrichment_cache)

    elapsed = time.time() - start_time
    print(f"\nTotal ETL time: {elapsed/60:.1f} minutes")


if __name__ == "__main__":
    main()
