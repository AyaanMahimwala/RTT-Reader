"""
Shared agent logic: Anthropic client, session management, system prompt, tool-use loop.

Used by both api.py (FastAPI) and telegram_bot.py (Telegram).
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from dotenv import load_dotenv

from db import TOOLS, execute_tool, get_schema, list_memories

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SONNET_MODEL = "claude-sonnet-4-20250514"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Pre-load schema for the system prompt
_schema_cache = None


def _get_schema_text():
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = get_schema()
    return _schema_cache


# ──────────────────────────────────────────────
# Session Management
# ──────────────────────────────────────────────

_sessions: Dict[str, dict] = {}  # session_id → {"messages": [...], "last_active": float}
SESSION_TTL = 3600  # 1 hour


def _prune_sessions():
    """Remove expired sessions."""
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["last_active"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]


def _get_or_create_session(session_id: Optional[str]) -> Tuple[str, list]:
    """Return (session_id, messages). Creates new session if needed."""
    _prune_sessions()

    if session_id and session_id in _sessions:
        session = _sessions[session_id]
        session["last_active"] = time.time()
        return session_id, session["messages"]

    # Create new session
    new_id = "s_" + uuid.uuid4().hex[:8]
    _sessions[new_id] = {"messages": [], "last_active": time.time()}
    return new_id, _sessions[new_id]["messages"]


def reset_session(session_id: str) -> None:
    """Remove a session so the next call creates a fresh one."""
    _sessions.pop(session_id, None)


# ──────────────────────────────────────────────
# Memory-Aware System Prompt
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are a personal calendar analyst. The user meticulously tracks every activity in their life as calendar events. You have access to a SQLite database of ~8,224 events from May 2024 to February 2026, enriched with structured fields, AND a vector store of ~14,585 sub-activities for semantic search.

YOU HAVE TWO SEARCH MODALITIES:

1. SQL (run_sql) — Use for QUANTITATIVE questions: counts, totals, averages, rankings, time ranges, percentages, and anything that can be precisely answered with structured data.
   Examples: "most productive month", "total hours with Aarti", "average wake-up time", "top 5 people by time spent"

2. Semantic search (semantic_search) — Use for QUALITATIVE questions: feelings, vibes, similarity, pattern discovery, or when the user uses vague/subjective language.
   Examples: "times I was in creative flow", "adventurous weekends", "when I felt recharged", "events that felt like my best days", "when I was in a rut"

3. Find similar (find_similar_events) — Use for "more like this" queries. First find an event_id (via SQL or semantic search), then find similar events.

You can use BOTH in the same answer — e.g., semantic_search to find relevant events, then run_sql to aggregate stats about those events.

SQL QUERYING TIPS:
- ALWAYS call get_schema first to understand the database structure and available categories.
- Use LIKE '%keyword%' for searching within comma-separated fields (categories, people, locations).
- The sub_activities table decomposes compound events — use it when looking for specific activities.
- The event_people table has one row per person per event — use it for people-related queries.
- duration_minutes tells you how long each event lasted — use SUM(duration_minutes) for time-spent queries.
- start_hour is a decimal (14.5 = 2:30 PM) — useful for "what time do I usually..." queries.
- ALWAYS use the day_of_week column from the database when reporting days. NEVER compute or guess what day of the week a date falls on — the database is the source of truth.
- When reporting dates to the user, always SELECT the day_of_week column alongside the date and use it in your response.
- Run exploratory queries first if needed, then follow up with specific ones.

SEMANTIC SEARCH TIPS:
- Write your query as a natural, descriptive phrase — the richer the better.
- Use metadata filters to narrow results (year, month, category, mood, etc.) when appropriate.
- Similarity scores closer to 1.0 are better matches.
- Results include the parent event summary for context.

MEMORY & LEARNING:
- You have a persistent memory system. Facts you've learned about this user are shown below.
- ALWAYS check your memories before interpreting ambiguous terms (names, places, abbreviations).
- When the user corrects you, use save_memory to record the correction, then re-answer.
- When the user says "remember that..." or similar, use save_memory to persist it.
- OCCASIONALLY (not every query), if you notice a significant pattern — a relationship change,
  a new routine forming, a major life event — ask ONE brief follow-up question at the end of
  your answer. If confirmed, save it. If dismissed, move on.
- Significant patterns: person frequency changes, new recurring locations, dramatic metric shifts.
- Do NOT ask follow-ups on routine queries or about things already in your memories.
- Do not save trivial or obvious facts. Only save things that change how you interpret future queries.

GENERAL:
- Always ground your answers in actual data — cite numbers, dates, and specific events.
- Be conversational and insightful. If you notice interesting patterns, mention them.
- When the user asks about productivity, consider both is_productive and work_depth fields.
- For time-based analysis, use year, month, day_of_week, start_hour as needed."""


def _get_memory_prompt():
    """Load all memories, group by category, format as bullet lists."""
    memories = list_memories()
    if not memories:
        return ""

    grouped: Dict[str, List[str]] = {}
    for m in memories:
        cat = m.get("category", "context")
        grouped.setdefault(cat, []).append(m["text"])

    lines = ["\nUSER MEMORY (facts you've learned about this user):"]
    category_labels = {
        "correction": "Corrections",
        "terminology": "Terminology",
        "relationship": "Relationships",
        "preference": "Preferences",
        "life_event": "Life Events",
        "routine": "Routines",
        "context": "Context",
    }
    for cat, label in category_labels.items():
        if cat in grouped:
            lines.append(f"{label}:")
            for text in grouped[cat]:
                lines.append(f"  - {text}")

    return "\n".join(lines)


def _mini_calendar(now: datetime) -> str:
    """Build a 3-week mini calendar (last week, this week, next week) so the
    model can resolve relative dates like 'last Monday' by lookup instead of math."""
    today = now.date()
    # Start from Monday of last week
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=1)

    lines = ["CALENDAR (use this to resolve relative dates):"]
    for week in range(3):
        week_start = start + timedelta(weeks=week)
        days = []
        for d in range(7):
            day = week_start + timedelta(days=d)
            marker = " <-- TODAY" if day == today else ""
            days.append(f"  {day.strftime('%a %b %d')}{marker}")
        lines.append("\n".join(days))
    return "\n".join(lines)


def _build_system_prompt():
    """Build the full system prompt with schema and memories."""
    tz_name = os.getenv("YOUR_TIMEZONE", "America/Los_Angeles")
    now = datetime.now(ZoneInfo(tz_name))
    date_str = now.strftime("%A, %B %d, %Y %I:%M %p")
    return (
        SYSTEM_PROMPT
        + f"\n\nCURRENT DATE: {date_str} ({tz_name})"
        + "\n\n" + _mini_calendar(now)
        + "\n\nDATABASE SCHEMA:\n" + _get_schema_text()
        + _get_memory_prompt()
    )


# ──────────────────────────────────────────────
# Core Agent Loop
# ──────────────────────────────────────────────

def run_agent(question: str, session_id: Optional[str] = None) -> dict:
    """Run the agent tool-use loop and return structured result.

    Returns {"answer": str, "sql_queries": list, "data": list, "session_id": str}
    """
    sql_queries = []
    all_data = []

    # Get or create session
    session_id, session_messages = _get_or_create_session(session_id)

    # Build dynamic system prompt with schema + memories
    system = _build_system_prompt()

    # Append the new user message to session history
    question = (question or "").strip() or "hi"
    session_messages.append({"role": "user", "content": question})

    # Build messages for the API call — full session history
    messages = list(session_messages)

    # Tool use loop — Claude can call tools iteratively
    max_iterations = 10
    for _ in range(max_iterations):
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Check if Claude wants to use tools
        if response.stop_reason == "tool_use":
            # Process all tool calls in this response
            assistant_content = response.content
            tool_results = []

            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input

                    # Track SQL queries
                    if tool_name == "run_sql" and "query" in tool_input:
                        sql_queries.append(tool_input["query"])

                    # Execute the tool
                    try:
                        result = execute_tool(tool_name, tool_input)
                        # Track data from SQL queries
                        if tool_name == "run_sql":
                            try:
                                parsed = json.loads(result)
                                if isinstance(parsed, list):
                                    all_data.extend(parsed[:50])
                            except (json.JSONDecodeError, TypeError):
                                pass
                    except Exception as e:
                        result = f"Error: {e}"

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result or "(empty)",
                        }
                    )

            # Add assistant message and tool results to conversation
            messages.append({"role": "assistant", "content": assistant_content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                # Shouldn't happen, but prevent empty user message
                break

        else:
            # Claude is done — extract the text answer
            answer = ""
            for block in response.content:
                if hasattr(block, "text"):
                    answer += block.text
            break
    else:
        answer = "I wasn't able to fully answer your question within the iteration limit. Please try rephrasing."

    # Persist only the final exchange in session history (not tool-use intermediates)
    session_messages.append({"role": "assistant", "content": answer})

    return {
        "answer": answer,
        "sql_queries": sql_queries,
        "data": all_data[:100],
        "session_id": session_id,
    }
