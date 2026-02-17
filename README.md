# RTT-Reader

A natural language interface for querying personal calendar data. Ask questions like "Who did I hang out with the most?" or "When was I in a rut?" and get data-backed conversational answers.

Built with a hybrid SQL + vector search backend — structured queries for quantitative questions, semantic search for vibes-based ones.

![Screenshot](screenshot.png)

## How It Works

```
Google Calendar → data-extract.py → calendar_raw_full.csv
                                          │
                                      etl.py (LLM enrichment + embedding)
                                          │
                              ┌───────────┴───────────┐
                              │                       │
                        calendar.db             calendar_vectors/
                        (SQLite)                (LanceDB)
                              │                       │
                              └───────────┬───────────┘
                                          │
                                       api.py (FastAPI + Claude tool_use)
                                          │
                                   static/index.html (Chat UI)
```

1. **Extract** — Pull all events from Google Calendar via service account
2. **Enrich** — Two-pass LLM pipeline discovers categories from the data, then extracts structured fields (people, locations, mood, work depth, productivity) for every event
3. **Embed** — Sub-activities are embedded into a vector store for semantic search
4. **Query** — Claude picks the right search modality per question (SQL for counts/rankings, vectors for vibes/similarity), iterates with tool calls, and returns grounded answers

## Two Search Modalities

| Modality | Best for | Example |
|----------|----------|---------|
| **SQL** (`run_sql`) | Counts, totals, averages, rankings | "Most productive month", "Top 5 people by time spent" |
| **Semantic** (`semantic_search`) | Vibes, feelings, similarity, patterns | "When was I in a rut?", "Times I was in creative flow" |
| **Both** | Complex questions | Semantic search to find events, then SQL to aggregate stats |

## Setup

### Prerequisites

- Python 3.9+
- Google Calendar with a service account configured for read access
- API keys: Anthropic (Claude), OpenAI (embeddings)

### Installation

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file:

```
CALENDAR_ID=your-calendar-id@gmail.com
SERVICE_ACCOUNT_FILE=your-service-account.json
YOUR_TIMEZONE=America/Chicago
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

### Build the Database

```bash
# 1. Extract events from Google Calendar
python data-extract.py

# 2. Run the ETL pipeline (enrichment + vector store)
#    First run is slow due to LLM calls; subsequent runs are cached
python etl.py
```

### Run the Server

```bash
uvicorn api:app --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## ETL Pipeline

The enrichment pipeline runs in three passes:

**Pass 1: Category Discovery** — Events are sent to Claude Haiku in batches. The LLM freely tags each event, then a Sonnet call consolidates the top tags into a clean taxonomy. Categories are discovered from the data, not hand-picked.

**Pass 2: Structured Enrichment** — Using the discovered taxonomy, each event is enriched with: sub-activities (compound event decomposition), people, locations, categories, work depth, mood, productivity, and wasted time flags.

**Pass 3: Vector Embedding** — All sub-activities are embedded using OpenAI's `text-embedding-3-small` model with a selective prose strategy — rich enough for semantic matching, without numeric noise that dilutes signal.

Both LLM passes use resume-safe JSON caches, so if the process crashes midway it picks up where it left off.

## Memory System

Claude learns about you as you chat. Corrections, terminology, relationships, and preferences are saved to persistent memory and included in future conversations. Memories are browsable and deletable from the sidebar.

## Project Structure

| File | Purpose |
|------|---------|
| `data-extract.py` | Google Calendar API → CSV extraction |
| `etl.py` | Three-pass LLM enrichment + vector store creation |
| `db.py` | Database helpers, vector search, memory store, Claude tool schemas |
| `api.py` | FastAPI server with Claude tool_use loop |
| `static/index.html` | Single-page chat UI with sidebar |
| `taxonomy.json` | LLM-discovered category taxonomy |
| `test_calendar.py` | Original CLI chatbot (date-range queries) |
| `verify.py` | Quick database verification script |
| `requirements.txt` | Python dependencies |
