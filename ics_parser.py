"""Parse ICS/iCal calendar export files into the CSV format used by the ETL pipeline.

Supports both .ics files and .zip archives (Google Calendar export produces a zip
containing one .ics per calendar).
"""

import csv
import io
import zipfile
from datetime import date, datetime


from icalendar import Calendar


def parse_ics_content(content: bytes) -> list[dict]:
    """Parse raw ICS bytes into event dicts matching data-extract.py output format."""
    cal = Calendar.from_ical(content)
    events = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("uid", "") or "")
        summary = str(component.get("summary", "") or "")
        description = str(component.get("description", "") or "")
        location = str(component.get("location", "") or "")
        status = str(component.get("status", "confirmed") or "confirmed")

        dtstart = component.get("dtstart")
        dtend = component.get("dtend")

        if dtstart is None:
            continue

        start_val = dtstart.dt
        end_val = dtend.dt if dtend else start_val

        # date objects (all-day events) → "YYYY-MM-DD"
        # datetime objects → ISO format with timezone
        if isinstance(start_val, date) and not isinstance(start_val, datetime):
            start_dt = start_val.isoformat()
            end_dt = end_val.isoformat() if isinstance(end_val, date) else end_val.isoformat()
        else:
            start_dt = start_val.isoformat()
            end_dt = end_val.isoformat()

        if not uid:
            continue

        events.append({
            "event_id": uid,
            "summary": summary,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "description": description,
            "location": location,
            "status": status.lower(),
        })

    return events


def parse_upload(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse an uploaded file (.ics or .zip) into event dicts.

    For .zip files, all .ics files inside are merged into one event list.
    """
    filename_lower = filename.lower()

    if filename_lower.endswith(".zip"):
        all_events = []
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".ics"):
                    ics_content = zf.read(name)
                    all_events.extend(parse_ics_content(ics_content))
        return all_events
    elif filename_lower.endswith(".ics"):
        return parse_ics_content(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {filename}. Please send a .ics or .zip file.")


def events_to_csv(events: list[dict], output_path: str) -> None:
    """Write events to CSV in the format expected by etl.py."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["event_id", "summary", "start_dt", "end_dt", "description", "location", "status"])
        for ev in events:
            writer.writerow([
                ev["event_id"],
                ev["summary"],
                ev["start_dt"],
                ev["end_dt"],
                ev["description"],
                ev["location"],
                ev["status"],
            ])
