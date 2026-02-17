from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime
from anthropic import Anthropic
import json
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

# Get config from .env
CALENDAR_ID = os.getenv('CALENDAR_ID')
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
YOUR_TIMEZONE = os.getenv('YOUR_TIMEZONE')
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# Initialize APIs
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=credentials)
claude_client = Anthropic(api_key=ANTHROPIC_API_KEY)

def get_calendar_events(start_date, end_date):
    """Fetch ALL events between start_date and end_date in your local timezone"""
    # Create timezone-aware datetime objects
    tz = ZoneInfo(YOUR_TIMEZONE)
    
    # Parse the date strings and make them timezone-aware
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=tz)
    # End of day (23:59:59)
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=tz)
    
    # Convert to ISO format (includes timezone offset)
    time_min = start_dt.isoformat()
    time_max = end_dt.isoformat()
    
    print(f"  📅 Searching: {start_date} to {end_date}")
    
    all_events = []
    page_token = None
    
    while True:
        events_result = calendar_service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=2500,  # Max allowed per request
            singleEvents=True,
            orderBy='startTime',
            pageToken=page_token
        ).execute()
        
        events = events_result.get('items', [])
        all_events.extend(events)
        
        # Check if there are more pages
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break
        
        print(f"  📄 Fetched {len(all_events)} events so far...")
    
    return all_events

def ask_claude_for_date_range(user_query, today):
    """Ask Claude to extract the date range from user query"""
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""Today's date is {today}. The current year is 2025.

User query: "{user_query}"

Extract the date range this query is asking about. Respond ONLY with valid JSON:
{{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}

Examples:
- "What did I do yesterday?" → {{"start_date": "2025-12-19", "end_date": "2025-12-19"}}
- "Show me last week" → {{"start_date": "2025-12-13", "end_date": "2025-12-19"}}
- "What about 6 months ago?" → {{"start_date": "2025-06-20", "end_date": "2025-06-20"}}
- "What was I doing today last year?" → {{"start_date": "2024-12-20", "end_date": "2024-12-20"}}
"""
        }]
    )
    
    # Extract JSON from Claude's response
    response_text = response.content[0].text
    return json.loads(response_text)

def ask_claude_to_answer(user_query, events, date_range):
    """Give Claude the events and ask it to answer the user's question"""
    # Format events nicely for Claude
    events_text = ""
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        summary = event.get('summary', 'No title')
        description = event.get('description', '')
        events_text += f"- {start}: {summary}\n"
        if description:
            events_text += f"  Description: {description}\n"
    
    if not events_text:
        events_text = "No events found for this time period."
    
    # Get current date for context
    tz = ZoneInfo(YOUR_TIMEZONE)
    today = datetime.now(tz).strftime('%Y-%m-%d')
    
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""Today's date is {today}. The current year is 2025.

User asked: "{user_query}"

You searched their calendar for: {date_range['start_date']} to {date_range['end_date']}

Here are their calendar events from that time period:
{events_text}

Answer their question in a friendly, conversational way. Be aware of what year we're currently in (2025) when interpreting dates."""
        }]
    )
    
    return response.content[0].text

def chat_with_calendar(user_query):
    """Main chatbot function"""
    # Get today in your timezone
    tz = ZoneInfo(YOUR_TIMEZONE)
    today = datetime.now(tz).strftime('%Y-%m-%d')
    
    # Step 1: Extract date range
    print("\n🤔 Thinking...")
    date_range = ask_claude_for_date_range(user_query, today)
    
    # Step 2: Fetch calendar events
    events = get_calendar_events(date_range['start_date'], date_range['end_date'])
    print(f"  ✅ Found {len(events)} events")
    
    # Step 3: Get Claude's answer
    answer = ask_claude_to_answer(user_query, events, date_range)
    
    return answer

def main():
    """Interactive chat loop"""
    print("=" * 60)
    print("📆 Calendar Chatbot - Powered by Claude")
    print("=" * 60)
    print("\nAsk me anything about your calendar!")
    print("Examples:")
    print("  - What did I do yesterday?")
    print("  - Show me last week")
    print("  - What was I doing 6 months ago?")
    print("  - Who did I hang out with in November?")
    print("\nType 'quit' or 'exit' to end the conversation.\n")
    
    while True:
        # Get user input
        user_input = input("You: ").strip()
        
        # Check for exit commands
        if user_input.lower() in ['quit', 'exit', 'bye']:
            print("\n👋 Goodbye! Thanks for chatting!")
            break
        
        # Skip empty input
        if not user_input:
            continue
        
        try:
            # Get response from chatbot
            response = chat_with_calendar(user_input)
            print(f"\n🤖 Claude: {response}\n")
            print("-" * 60)
        except Exception as e:
            print(f"\n❌ Error: {e}\n")
            print("Try rephrasing your question or check your API keys.")

if __name__ == "__main__":
    main()