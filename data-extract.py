import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from googleapiclient.discovery import build

# Load environment variables from your .env
load_dotenv()


def _fetch_events(service, calendar_id, time_min):
    """Paginated fetch of events from the Google Calendar API."""
    all_events = []
    page_token = None
    while True:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            singleEvents=True,
            orderBy='startTime',
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        batch = events_result.get('items', [])
        all_events.extend(batch)
        print(f"  Fetched {len(all_events)} events...")
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break
    return all_events


def get_raw_calendar_data_with_creds(start_date_str, credentials, calendar_id="primary"):
    """Fetch raw events using OAuth credentials.

    Accepts any credential object and paginates through all events.
    """
    service = build('calendar', 'v3', credentials=credentials)

    tz_name = os.getenv('YOUR_TIMEZONE', 'America/Los_Angeles')
    tz = ZoneInfo(tz_name)
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').replace(tzinfo=tz)
    time_min = start_dt.isoformat()

    print(f"OAuth fetch: {start_date_str} to present")
    return _fetch_events(service, calendar_id, time_min)


def export_to_csv(events, filename):
    """Writes the raw JSON fields to a CSV."""
    with open(filename, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        # Raw headers for our future brainstorming
        writer.writerow(['event_id', 'summary', 'start_dt', 'end_dt', 'description', 'location', 'status'])

        for event in events:
            # Handle both timed and all-day events
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))
            
            writer.writerow([
                event.get('id'),
                event.get('summary', ''),
                start,
                end,
                event.get('description', ''),
                event.get('location', ''),
                event.get('status', '')
            ])
