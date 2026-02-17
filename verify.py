"""Quick verification of the enriched database."""
from db import run_sql

print("=== SAMPLE ENRICHED EVENTS ===")
samples = run_sql("SELECT summary, categories, people, locations, work_depth, mood, is_productive, is_wasted_time FROM events WHERE people != '' ORDER BY RANDOM() LIMIT 5")
for s in samples:
    print(f'  Summary: {s["summary"]}')
    print(f'    categories={s["categories"]} | people={s["people"]} | locations={s["locations"]}')
    print(f'    work_depth={s["work_depth"]} | mood={s["mood"]} | productive={s["is_productive"]} | wasted={s["is_wasted_time"]}')
    print()

print("=== COMPOUND EVENT DECOMPOSITION ===")
compound = run_sql("SELECT e.summary, e.event_id FROM events e JOIN sub_activities sa ON e.event_id = sa.event_id GROUP BY e.event_id HAVING COUNT(*) > 3 ORDER BY RANDOM() LIMIT 2")
for c in compound:
    eid = c["event_id"]
    print(f'Event: "{c["summary"]}"')
    subs = run_sql(f"SELECT activity, category FROM sub_activities WHERE event_id = '{eid}'")
    for sub in subs:
        print(f'  - {sub["activity"]} ({sub["category"]})')
    print()

print("=== WASTED TIME EVENTS ===")
wasted = run_sql("SELECT summary, date, duration_minutes FROM events WHERE is_wasted_time = 1 ORDER BY RANDOM() LIMIT 5")
for w in wasted:
    print(f'  {w["date"]}: "{w["summary"]}" ({w["duration_minutes"]} min)')

print("\n=== WORK DEPTH DISTRIBUTION ===")
depths = run_sql("SELECT work_depth, COUNT(*) as cnt FROM events WHERE work_depth IS NOT NULL GROUP BY work_depth ORDER BY cnt DESC")
for d in depths:
    print(f'  {d["work_depth"]}: {d["cnt"]}')

print("\n=== MOOD DISTRIBUTION ===")
moods = run_sql("SELECT mood, COUNT(*) as cnt FROM events WHERE mood IS NOT NULL GROUP BY mood ORDER BY cnt DESC")
for m in moods:
    print(f'  {m["mood"]}: {m["cnt"]}')
