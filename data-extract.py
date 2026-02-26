import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load environment variables from your .env
load_dotenv()

CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
YOUR_TIMEZONE = os.getenv('YOUR_TIMEZONE')
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def get_raw_calendar_data(start_date_str):
    """
    Extracts ALL raw events from start_date_str to now.
    No transformations, just the source truth.
    """
    # 1. Initialize Service Account Credentials
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('calendar', 'v3', credentials=credentials)

    # 2. Setup Timezone and Start Boundary
    tz = ZoneInfo(YOUR_TIMEZONE)
    # Start of the target day in your local timezone
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').replace(tzinfo=tz)
    time_min = start_dt.isoformat()

    print(f"🚀 Starting Extraction: {start_date_str} to Present")
    
    all_events = []
    page_token = None
    
    # 3. Pagination Loop for 10k+ Events
    while True:
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            singleEvents=True,
            orderBy='startTime',
            maxResults=2500, # Efficient batch size
            pageToken=page_token
        ).execute()
        
        batch = events_result.get('items', [])
        all_events.extend(batch)
        print(f"  📄 Fetched {len(all_events)} events...")
        
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break
            
    return all_events

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
    """Fetch raw events using pre-built OAuth credentials (per-user flow).

    Same logic as get_raw_calendar_data() but accepts any credential object
    instead of relying on the global service account.
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
        writer.writerow(['event_id', 'summary', 'start_dt', 'end_dt', 'description', 'location', 'status', 'last_modified'])

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
                event.get('status', ''),
                event.get('updated', ''),
            ])

if __name__ == "__main__":
    # Target date: May 8, 2024
    RAW_DATA_FILE = "calendar_raw_full.csv"
    
    try:
        raw_events = get_raw_calendar_data("2024-05-08")
        export_to_csv(raw_events, RAW_DATA_FILE)
        print(f"\n✅ Success! {len(raw_events)} events saved to {RAW_DATA_FILE}")
    except Exception as e:
        print(f"\n❌ ETL Error: {e}")