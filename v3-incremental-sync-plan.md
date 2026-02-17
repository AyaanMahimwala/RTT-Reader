# V3: Incremental Calendar Sync — Exploration Plan

## Context

The current pipeline requires a full re-run to pick up new calendar events — re-extracting all 8,200+ events from Google Calendar, then rebuilding the SQLite database and LanceDB vector store from scratch. This means new events you add to your calendar never appear in the query system unless you manually re-run the ~2 hour ETL.

The goal is to add an incremental sync that fetches only new/changed events from Google Calendar and surgically updates both databases, so your query system stays current with minimal cost and time.

---

## Sync Strategy: `updatedMin` Parameter

Google Calendar's `events.list` API accepts an `updatedMin` parameter — it returns only events whose `updated` timestamp is after the given time. Combined with `showDeleted=True`, this catches new events, modified events, and deleted events in a single API call. This works with the existing `singleEvents=True` + `orderBy='startTime'` query style.

We'd track the last sync time in a `sync_state.json` file. On each incremental sync:
1. Fetch events with `updatedMin=last_sync_time` + `showDeleted=True`
2. Diff against existing data to classify: new, modified, or deleted
3. Run LLM enrichment only on new/modified events (caches already skip processed events)
4. Upsert into SQLite and LanceDB (not drop-and-recreate)

---

## Implementation Plan

### 1. Modify `data-extract.py`

**Add incremental fetch + CSV merge capabilities:**

- Add `sync_state.json` load/save functions to track `last_sync_time`
- Add `get_incremental_calendar_data(updated_min)` — same as `get_raw_calendar_data` but adds `updatedMin` and `showDeleted=True` params to the API call
- Add `merge_into_full_csv(active_events, deleted_events)` — reads existing CSV into a dict keyed by `event_id`, applies inserts/updates, removes deleted IDs, rewrites sorted by `start_dt`
- Add `argparse` to `__main__`: `--full` forces full extraction, default is incremental (falls back to full if no `sync_state.json` exists)

### 2. Create `sync.py` (new file — the orchestrator)

**Single entry point that ties extraction and ETL together:**

```
python sync.py           # Incremental (default)
python sync.py --full    # Full rebuild
```

Flow:
1. Snapshot existing event fingerprints (`event_id → summary|start_dt|end_dt`)
2. Run incremental extraction (updates `calendar_raw_full.csv` in-place)
3. Snapshot new fingerprints, compute diff → `added_ids`, `modified_ids`, `deleted_ids`
4. If no changes → exit early ("Database is up to date")
5. If changes → call `etl.run_incremental_etl(added, modified, deleted)`

### 3. Modify `etl.py`

**Add incremental ETL path alongside existing full-rebuild `main()`:**

- **`run_incremental_etl(new_ids, deleted_ids, modified_ids)`** — new entry point:
  - Evict modified events from `discovery_cache.json` and `enrichment_cache.json` so they get re-processed
  - Run Pass 1 & 2 on the full event list (existing caching logic skips already-cached events, so only new/modified events hit the LLM)
  - Call `upsert_database()` and `upsert_vector_store()` for surgical updates

- **`evict_from_caches(event_ids)`** — removes stale entries from both LLM caches for modified events

- **`upsert_database(events, enrichment_cache, affected_ids, deleted_ids)`** — instead of `os.remove(DB_FILE)`:
  - `DELETE FROM events/sub_activities/event_people WHERE event_id = ?` for deleted + modified events
  - `INSERT OR REPLACE` for new + modified events
  - Falls back to `create_database()` if `calendar.db` doesn't exist yet

- **`upsert_vector_store(events, enrichment_cache, affected_ids, deleted_ids)`** — instead of `db.drop_table()`:
  - `table.delete(f"event_id IN (...)")` for deleted + modified events
  - Build records, embed, `table.add(records)` for new + modified events
  - Falls back to `create_vector_store()` if table doesn't exist yet

The existing `main()`, `create_database()`, and `create_vector_store()` remain untouched for `--full` rebuilds.

### 4. Modify `api.py`

**Add two endpoints:**

- `POST /sync` — triggers `python sync.py` via subprocess, returns stdout/stderr. Invalidates `_schema_cache` after sync.
- `GET /sync/status` — reads `sync_state.json` and returns last sync time

### 5. Modify `db.py`

**Add `invalidate_caches()`** — resets the `_vector_table` singleton so LanceDB picks up newly added rows after a sync.

### 6. Modify `static/index.html`

**Add sync button in sidebar header:**

- "Sync Calendar" button below "+ New Chat"
- Shows "Syncing..." while running, then success/failure status
- On page load, fetch `/sync/status` and display "Last synced: ..." below the button

---

## Files Changed

| File | Change |
|------|--------|
| `data-extract.py` | Add incremental fetch, CSV merge, sync state tracking, argparse |
| `sync.py` (**new**) | Orchestrator: diff detection + calls ETL |
| `etl.py` | Add `run_incremental_etl`, `upsert_database`, `upsert_vector_store`, `evict_from_caches` |
| `api.py` | Add `POST /sync` and `GET /sync/status` endpoints |
| `db.py` | Add `invalidate_caches()` |
| `static/index.html` | Add sync button + status display in sidebar |
| `sync_state.json` (**auto-generated**) | Tracks `last_sync_time` |

---

## What's Already Incremental (No Changes Needed)

The existing codebase has good incremental foundations:
- **Pass 1 discovery** (`discovery_cache.json`) — already skips cached `event_id`s
- **Pass 2 enrichment** (`enrichment_cache.json`) — already skips cached `event_id`s
- **Taxonomy** (`taxonomy.json`) — already reuses existing file, not regenerated
- **SQLite events table** — already uses `INSERT OR REPLACE` on primary key

What currently blocks incremental:
- `data-extract.py` always fetches ALL events from 2024-05-08
- `etl.py` does `os.remove(DB_FILE)` before SQLite loading
- `etl.py` does `db.drop_table("sub_activities")` before vector store creation

---

## Edge Cases

- **Modified events**: Detected by fingerprint comparison (`summary|start_dt|end_dt`). Stale LLM cache entries evicted and re-enriched.
- **Deleted events**: Caught via `showDeleted=True` in API call. Removed from CSV, SQLite (all 3 tables), and LanceDB.
- **Taxonomy**: Not re-generated on incremental syncs — 17 categories are stable across 8,000+ events. Use `--full` if ever needed.
- **First run (no sync_state.json)**: Falls back to full extraction + full ETL automatically.
- **No changes detected**: Exits early with "Database is up to date" message.
- **Idempotency**: Running sync twice with no new events results in zero API calls and zero database changes.

---

## Cost

- Incremental sync for ~10 new events: ~$0.001 (1 Haiku batch each for Pass 1 + 2, 1 OpenAI embedding batch)
- Full rebuild: same as before (~$1-1.50)

---

## Entry Points

For maximum flexibility, the sync could be triggered through:

1. **CLI** (primary): `python sync.py` or `python sync.py --full`
2. **UI button**: "Sync Calendar" button in the sidebar
3. **API endpoint**: `POST /sync`
4. **Cron** (optional): `0 */6 * * * cd /path/to/RTT-Reader && python sync.py`

---

## Verification Steps

1. `python sync.py --full` — baseline (identical to current `data-extract.py` + `etl.py`)
2. `python sync.py` immediately after — "No changes. Database is up to date."
3. Add a test event in Google Calendar → `python sync.py` → "1 new, 0 modified, 0 deleted"
4. Query the new event via UI to confirm it's in both SQLite and vector search
5. Modify the test event's summary → sync → "0 new, 1 modified, 0 deleted"
6. Delete the test event → sync → "0 new, 0 modified, 1 deleted"
7. Test the UI sync button via `POST /sync`
8. Verify `/sync/status` returns last sync time
