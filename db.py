"""
Database helpers and Claude tool definitions for the calendar query API.
Includes both SQLite (structured queries) and LanceDB (semantic search).

All functions accept an optional `data_dir` parameter for multi-user support.
When data_dir is None, the default DATA_DIR is used (backward compatible).
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime

import lancedb
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DB_FILE = os.path.join(_DATA_DIR, "calendar.db")
TAXONOMY_FILE = os.path.join(_DATA_DIR, "taxonomy.json")
VECTOR_DIR = os.path.join(_DATA_DIR, "calendar_vectors")
MEMORY_FILE = os.path.join(_DATA_DIR, "memory.json")
EMBEDDING_MODEL = "text-embedding-3-small"

_openai_client = None
_vector_tables: dict[str, object] = {}  # vector_dir_path → LanceDB table


def _db_path(data_dir=None):
    return os.path.join(data_dir, "calendar.db") if data_dir else DB_FILE


def _vector_dir(data_dir=None):
    return os.path.join(data_dir, "calendar_vectors") if data_dir else VECTOR_DIR


def _memory_path(data_dir=None):
    return os.path.join(data_dir, "memory.json") if data_dir else MEMORY_FILE


def _taxonomy_path(data_dir=None):
    return os.path.join(data_dir, "taxonomy.json") if data_dir else TAXONOMY_FILE


def _get_openai():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def _get_vector_table(data_dir=None):
    vdir = _vector_dir(data_dir)
    if vdir not in _vector_tables:
        db = lancedb.connect(vdir)
        _vector_tables[vdir] = db.open_table("sub_activities")
    return _vector_tables[vdir]


def invalidate_vector_cache(data_dir=None):
    """Reset the vector table singleton so LanceDB picks up newly added rows."""
    vdir = _vector_dir(data_dir)
    _vector_tables.pop(vdir, None)


def _embed_query(text):
    """Embed a single query string."""
    response = _get_openai().embeddings.create(model=EMBEDDING_MODEL, input=[text])
    return response.data[0].embedding


def get_connection(data_dir=None):
    conn = sqlite3.connect(_db_path(data_dir))
    conn.row_factory = sqlite3.Row
    return conn


def run_sql(query, max_rows=200, data_dir=None):
    """Execute a read-only SQL query and return results as list of dicts."""
    conn = get_connection(data_dir)
    try:
        cursor = conn.execute(query)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(max_rows)
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def get_schema(data_dir=None):
    """Return CREATE TABLE statements, column descriptions, and category taxonomy."""
    conn = get_connection(data_dir)
    try:
        cursor = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")
        schemas = [row[0] for row in cursor.fetchall() if row[0]]
    finally:
        conn.close()

    column_docs = {
        "events.event_id": "Unique event identifier",
        "events.summary": "Original event title/summary text",
        "events.start_dt": "ISO datetime with timezone offset",
        "events.end_dt": "ISO datetime with timezone offset",
        "events.date": "YYYY-MM-DD date string",
        "events.year": "Integer year (2024, 2025, 2026)",
        "events.month": "Integer month (1-12)",
        "events.day_of_week": "Full day name (Monday, Tuesday, etc.)",
        "events.start_hour": "Decimal hour of day (14.5 = 2:30 PM)",
        "events.duration_minutes": "Event duration in minutes",
        "events.categories": "Comma-separated category names from taxonomy",
        "events.people": "Comma-separated people names",
        "events.locations": "Comma-separated location names",
        "events.work_depth": "deep, medium, shallow, meeting, or NULL",
        "events.mood": "positive, neutral, negative, or NULL",
        "events.is_productive": "1 if productive time, 0 otherwise",
        "events.is_wasted_time": "1 if wasted/unproductive time, 0 otherwise",
        "events.description": "Event description field (often empty)",
        "events.location_raw": "Raw location field from Google Calendar (often empty)",
        "sub_activities.activity": "Individual activity from compound event decomposition",
        "sub_activities.category": "Primary category for this sub-activity",
        "sub_activities.people": "People involved in this sub-activity",
        "event_people.person": "Individual person name (one row per person per event)",
    }

    taxonomy_info = ""
    tax_file = _taxonomy_path(data_dir)
    if os.path.exists(tax_file):
        with open(tax_file) as f:
            taxonomy = json.load(f)
        cats = []
        for cat in taxonomy.get("categories", []):
            cats.append(f"  - {cat['name']}: {cat['description']}")
        taxonomy_info = "\n\nCATEGORY TAXONOMY:\n" + "\n".join(cats)

    col_docs_text = "\n".join(f"  {k}: {v}" for k, v in column_docs.items())

    return (
        "TABLES:\n"
        + "\n\n".join(schemas)
        + "\n\nCOLUMN DESCRIPTIONS:\n"
        + col_docs_text
        + taxonomy_info
    )


def get_sample_rows(n=10, data_dir=None):
    """Return n sample rows from the events table with all fields."""
    return run_sql(f"SELECT * FROM events ORDER BY RANDOM() LIMIT {n}", data_dir=data_dir)


def get_category_distribution(data_dir=None):
    """Show how many events use each category."""
    conn = get_connection(data_dir)
    try:
        cursor = conn.execute("SELECT categories FROM events WHERE categories != ''")
        counts = {}
        for row in cursor:
            for cat in row[0].split(","):
                cat = cat.strip()
                if cat:
                    counts[cat] = counts.get(cat, 0) + 1
        return sorted(counts.items(), key=lambda x: -x[1])
    finally:
        conn.close()


def get_people_frequency(data_dir=None):
    """Show all people and their event counts."""
    return run_sql(
        "SELECT person, COUNT(*) as event_count FROM event_people "
        "GROUP BY person ORDER BY event_count DESC",
        data_dir=data_dir,
    )


# ──────────────────────────────────────────────
# Vector Search Functions
# ──────────────────────────────────────────────

def semantic_search(query, n=20, filters=None, data_dir=None):
    """Search sub-activities by semantic similarity. Returns top N matches."""
    table = _get_vector_table(data_dir)
    query_vector = _embed_query(query)

    search = table.search(query_vector).limit(n)

    # Build WHERE clause from filters
    if filters:
        where_parts = []
        for key, value in filters.items():
            if value is None or value == "":
                continue
            if key in ("year", "month"):
                where_parts.append(f"{key} = {int(value)}")
            elif key == "is_productive":
                where_parts.append(f"is_productive = {str(value).lower()}")
            elif key == "is_wasted_time":
                where_parts.append(f"is_wasted_time = {str(value).lower()}")
            elif key == "min_start_hour":
                where_parts.append(f"start_hour >= {float(value)}")
            elif key == "max_start_hour":
                where_parts.append(f"start_hour <= {float(value)}")
            elif key == "person":
                where_parts.append(f"people LIKE '%{value}%'")
            else:
                where_parts.append(f"{key} = '{value}'")

        if where_parts:
            search = search.where(" AND ".join(where_parts))

    results = search.to_list()

    # Format results — drop the raw vector, add distance score
    formatted = []
    for r in results:
        formatted.append({
            "activity": r["activity"],
            "parent_summary": r["parent_summary"],
            "event_id": r["event_id"],
            "category": r["category"],
            "date": r["date"],
            "day_of_week": r["day_of_week"],
            "start_hour": r["start_hour"],
            "duration_minutes": r["duration_minutes"],
            "people": r["people"],
            "locations": r["locations"],
            "mood": r["mood"],
            "work_depth": r["work_depth"],
            "is_productive": r["is_productive"],
            "similarity_score": round(1 - r["_distance"], 4),
        })
    return formatted


def find_similar_events(event_id, n=10, data_dir=None):
    """Find sub-activities most similar to a given event's sub-activities."""
    table = _get_vector_table(data_dir)

    # Get the vectors for this event's sub-activities
    event_rows = table.search().where(f"event_id = '{event_id}'").limit(100).to_list()

    if not event_rows:
        return {"error": f"No sub-activities found for event_id: {event_id}"}

    # Use the first sub-activity's vector as the query
    query_vector = event_rows[0]["vector"]

    # Search excluding the source event
    results = (
        table.search(query_vector)
        .where(f"event_id != '{event_id}'")
        .limit(n)
        .to_list()
    )

    formatted = []
    for r in results:
        formatted.append({
            "activity": r["activity"],
            "parent_summary": r["parent_summary"],
            "event_id": r["event_id"],
            "category": r["category"],
            "date": r["date"],
            "day_of_week": r["day_of_week"],
            "people": r["people"],
            "locations": r["locations"],
            "mood": r["mood"],
            "similarity_score": round(1 - r["_distance"], 4),
        })
    return formatted


# ──────────────────────────────────────────────
# Memory Store Functions
# ──────────────────────────────────────────────

MEMORY_CATEGORIES = [
    "correction", "terminology", "relationship",
    "preference", "life_event", "routine", "context",
]


def _load_memories(data_dir=None):
    """Read memory.json or return empty structure."""
    path = _memory_path(data_dir)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"memories": []}


def _save_memories(data, data_dir=None):
    """Write memory.json."""
    path = _memory_path(data_dir)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def save_memory(text, category, tags=None, data_dir=None):
    """Append a new memory and return confirmation."""
    data = _load_memories(data_dir)
    memory_id = "m_" + uuid.uuid4().hex[:6]
    memory = {
        "id": memory_id,
        "text": text,
        "category": category if category in MEMORY_CATEGORIES else "context",
        "tags": [t.lower() for t in (tags or [])],
        "created_at": datetime.now().isoformat(),
    }
    data["memories"].append(memory)
    _save_memories(data, data_dir)
    return {"status": "saved", "id": memory_id}


def search_memories(query, data_dir=None):
    """Case-insensitive match against text and tags."""
    data = _load_memories(data_dir)
    query_lower = query.lower()
    results = []
    for m in data["memories"]:
        if query_lower in m["text"].lower():
            results.append(m)
        elif any(query_lower in tag for tag in m.get("tags", [])):
            results.append(m)
    return results


def list_memories(category=None, data_dir=None):
    """Return all memories, optionally filtered by category."""
    data = _load_memories(data_dir)
    if category:
        return [m for m in data["memories"] if m["category"] == category]
    return data["memories"]


def delete_memory(memory_id, data_dir=None):
    """Delete a memory by id. Returns True if found and deleted, False otherwise."""
    data = _load_memories(data_dir)
    original_len = len(data["memories"])
    data["memories"] = [m for m in data["memories"] if m["id"] != memory_id]
    if len(data["memories"]) < original_len:
        _save_memories(data, data_dir)
        return True
    return False


# ──────────────────────────────────────────────
# Data Stats (for dynamic system prompt)
# ──────────────────────────────────────────────

def get_data_stats(data_dir=None):
    """Get event count, sub-activity count, date range, and people count."""
    try:
        conn = get_connection(data_dir)
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sub_count = conn.execute("SELECT COUNT(*) FROM sub_activities").fetchone()[0]
        date_range = conn.execute("SELECT MIN(date), MAX(date) FROM events").fetchone()
        people_count = conn.execute("SELECT COUNT(DISTINCT person) FROM event_people").fetchone()[0]
        conn.close()
        return {
            "event_count": event_count,
            "sub_activity_count": sub_count,
            "date_min": date_range[0] or "unknown",
            "date_max": date_range[1] or "unknown",
            "unique_people": people_count,
        }
    except Exception:
        return None


# ──────────────────────────────────────────────
# Claude Tool Definitions
# ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Execute a read-only SQL query against the calendar SQLite database. "
            "Returns up to 200 rows as a list of objects. Use this for all data queries. "
            "Use LIKE '%keyword%' to search within comma-separated fields (categories, people, locations). "
            "Tables: events, sub_activities, event_people."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A read-only SQL query (SELECT only)",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_schema",
        "description": (
            "Get the database schema including CREATE TABLE statements, column descriptions, "
            "and the category taxonomy. Call this first to understand the data structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_sample_rows",
        "description": "Get random sample rows from the events table to understand the data format and values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of sample rows to return (default 10)",
                    "default": 10,
                }
            },
        },
    },
    {
        "name": "get_category_distribution",
        "description": "Get a count of events per category. Useful for understanding what categories exist and their frequencies.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_people_frequency",
        "description": "Get all people mentioned in events and how many events they appear in, sorted by frequency.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Search sub-activities by semantic meaning using vector similarity. "
            "Use this for qualitative, vibes-based, or fuzzy queries — things like "
            "'times I was in creative flow', 'adventurous weekends', 'when I felt recharged', "
            "'events similar to a great day out'. Returns the most semantically similar "
            "sub-activities with parent event context and similarity scores. "
            "Supports optional metadata filters to narrow results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of what to search for",
                },
                "n": {
                    "type": "integer",
                    "description": "Number of results to return (default 20)",
                    "default": 20,
                },
                "filters": {
                    "type": "object",
                    "description": "Optional metadata filters to narrow results",
                    "properties": {
                        "category": {"type": "string", "description": "Exact category name (e.g. 'deep_work')"},
                        "year": {"type": "integer", "description": "Filter by year (2024, 2025, 2026)"},
                        "month": {"type": "integer", "description": "Filter by month (1-12)"},
                        "day_of_week": {"type": "string", "description": "Filter by day (e.g. 'Monday')"},
                        "mood": {"type": "string", "description": "Filter by mood (positive, neutral, negative)"},
                        "is_productive": {"type": "boolean", "description": "Filter by productivity"},
                        "is_wasted_time": {"type": "boolean", "description": "Filter by wasted time"},
                        "person": {"type": "string", "description": "Filter by person name (partial match)"},
                        "min_start_hour": {"type": "number", "description": "Minimum start hour (decimal, e.g. 6.0)"},
                        "max_start_hour": {"type": "number", "description": "Maximum start hour (decimal, e.g. 12.0)"},
                    },
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_similar_events",
        "description": (
            "Given a specific event_id, find the most similar sub-activities across the dataset. "
            "Use this for 'more like this' queries — e.g., 'find other days like that great Saturday' "
            "or 'what events were similar to the boys trip?'. First use run_sql or semantic_search "
            "to find the event_id, then use this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The event_id to find similar events for",
                },
                "n": {
                    "type": "integer",
                    "description": "Number of similar results to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save a fact about the user to persistent memory. Use this when: "
            "(1) the user corrects you about something, "
            "(2) the user confirms a pattern you noticed, or "
            "(3) the user says 'remember that...' or similar. "
            "Only save things that change how you interpret future queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The factual statement to remember",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "correction", "terminology", "relationship",
                        "preference", "life_event", "routine", "context",
                    ],
                    "description": "Category of the memory",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional lowercase keywords for searchability",
                },
            },
            "required": ["text", "category"],
        },
    },
    {
        "name": "search_memories",
        "description": (
            "Search persistent memories by text or tag match. "
            "Use this before interpreting ambiguous terms, names, or abbreviations "
            "to check if you have relevant context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to match against memory text and tags",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_memories",
        "description": "List all persistent memories, optionally filtered by category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "correction", "terminology", "relationship",
                        "preference", "life_event", "routine", "context",
                    ],
                    "description": "Optional category filter",
                },
            },
        },
    },
]


def execute_tool(tool_name, tool_input, data_dir=None):
    """Execute a tool by name and return the result as a string."""
    if tool_name == "run_sql":
        result = run_sql(tool_input["query"], data_dir=data_dir)
    elif tool_name == "get_schema":
        result = get_schema(data_dir)
    elif tool_name == "get_sample_rows":
        result = get_sample_rows(tool_input.get("n", 10), data_dir=data_dir)
    elif tool_name == "get_category_distribution":
        result = get_category_distribution(data_dir)
    elif tool_name == "get_people_frequency":
        result = get_people_frequency(data_dir)
    elif tool_name == "semantic_search":
        result = semantic_search(
            tool_input["query"],
            tool_input.get("n", 20),
            tool_input.get("filters"),
            data_dir=data_dir,
        )
    elif tool_name == "find_similar_events":
        result = find_similar_events(
            tool_input["event_id"],
            tool_input.get("n", 10),
            data_dir=data_dir,
        )
    elif tool_name == "save_memory":
        result = save_memory(
            tool_input["text"],
            tool_input["category"],
            tool_input.get("tags"),
            data_dir=data_dir,
        )
    elif tool_name == "search_memories":
        result = search_memories(tool_input["query"], data_dir=data_dir)
    elif tool_name == "list_memories":
        result = list_memories(tool_input.get("category"), data_dir=data_dir)
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    if isinstance(result, str):
        return result
    return json.dumps(result, default=str)
