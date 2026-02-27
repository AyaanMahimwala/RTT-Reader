# Telegram Bot — Usage Guide & Testing Methodology

## What This Bot Does

A multi-user Telegram bot that lets users upload Google Calendar exports (ICS/ZIP) or connect via Google OAuth, processes them through an LLM-powered ETL pipeline, and answers natural-language questions using hybrid search (SQL + vector/semantic).

---

## Prerequisites

Before running the bot, set these environment variables in `.env`:

| Variable | Purpose | How to get it |
|----------|---------|---------------|
| `TELEGRAM_BOT_TOKEN` | Bot API token | Create a bot via [@BotFather](https://t.me/BotFather) on Telegram |
| `TELEGRAM_USER_ID` | Your numeric Telegram ID (makes you admin) | Message [@userinfobot](https://t.me/userinfobot) on Telegram |
| `ANTHROPIC_API_KEY` | Claude API for agent + ETL | [Anthropic Console](https://console.anthropic.com) |
| `OPENAI_API_KEY` | Embeddings (text-embedding-3-small) | [OpenAI Platform](https://platform.openai.com) |
| `YOUR_TIMEZONE` | e.g. `America/Los_Angeles` | For date parsing |

### Admin sync (service account — optional)

| Variable | Purpose | How to get it |
|----------|---------|---------------|
| `SERVICE_ACCOUNT_FILE` | Google service account JSON | Google Cloud Console |
| `CALENDAR_ID` | Google Calendar email | Your Google Calendar settings |

### Per-user OAuth sync (optional)

| Variable | Purpose | How to get it |
|----------|---------|---------------|
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID | GCP Console > Credentials > "Web application" type |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret | Same as above |
| `OAUTH_REDIRECT_URI` | Callback URL | e.g. `https://your-app.up.railway.app/oauth/callback` |

---

## Running Locally

```bash
python telegram_bot.py
```

The bot uses **long-polling** (no webhooks needed) — works behind NAT/firewalls with no public URL.

When `GOOGLE_CLIENT_ID` is set, the bot also starts a uvicorn web server (on `PORT`, default 8000) to handle OAuth callbacks.

---

## Bot Commands

| Command | Who can use it | What it does |
|---------|---------------|--------------|
| `/start` | Anyone | Shows welcome message and available commands |
| `/register` | Unregistered users | Creates your user account and data directory |
| `/status` | Registered users | Shows registration status, event count, Google Calendar connection, and next steps |
| `/new` | Ready users | Resets your conversation session (start fresh) |
| `/sync` | Registered users | Syncs calendar from Google — admin uses service account, others use OAuth |
| `/process` | Registered users | Runs the ETL pipeline on uploaded calendar data |

---

## User Flows

### Non-Admin — Manual Upload

```
1. /start          -> See welcome message
2. /register       -> Create account (status: "registered")
3. Upload file     -> Send a .ics or .zip calendar export (status: "data_uploaded")
4. /process        -> Runs ETL enrichment pipeline (status: "processing" -> "ready")
5. Ask questions!  -> "Who did I hang out with the most?" etc.
```

### Non-Admin — Google OAuth Sync

```
1. /start          -> See welcome message
2. /register       -> Create account
3. /sync           -> Bot sends Google OAuth link
4. Click link      -> Authorize in browser -> redirected to callback
5. Bot sends       -> "Google Calendar connected! Synced 42 new events."
6. Ask questions!  -> Data is ready immediately, no /process needed
```

Subsequent `/sync` calls skip the OAuth step and sync directly using the stored token.

### Admin

1. Admin is **auto-registered** on bot startup with "ready" status
2. Admin data lives in the root `DATA_DIR` (backward compatible)
3. `/sync` uses the service account (if `SERVICE_ACCOUNT_FILE` is set)
4. Ask questions immediately — no file upload or `/process` needed if data is already seeded

### How to get your calendar export

- **Google Calendar**: Settings > Import & Export > Export — downloads a `.zip` with `.ics` files
- **Apple Calendar**: File > Export > Export... — saves a `.ics` file
- Send the file directly to the bot in Telegram

---

## Querying Your Calendar

Once your status is "ready", just send any text message. Examples:

- "Who did I hang out with the most last month?"
- "What did I do last weekend?"
- "When was I in a rut?"
- "How much time did I spend on deep work this week?"
- "Show me my most productive days"

The agent uses **hybrid search**: SQL for structured queries (counts, dates, durations) and vector/semantic search for vibes-based questions.

---

## Session Management

- Conversations are kept in memory for **1 hour** of inactivity
- Use `/new` to start a fresh conversation
- The agent remembers things you tell it across sessions (stored in `memory.json`)

---

## Multi-User Architecture

- Each user gets an isolated data directory: `{DATA_DIR}/users/{telegram_user_id}/`
- User registry stored in `users.json`
- Per-user databases, vectors, memories, caches, and OAuth tokens — fully isolated
- Admin uses root `DATA_DIR` for backward compatibility

---

## Deployment (Railway)

```bash
# Push to GitHub, then in Railway:
1. Create project -> link GitHub repo
2. Add persistent volume mounted at /data (1GB)
3. Set all env vars from .env
4. Deploy (auto-deploys on push)
```

Bot runs via: `python seed.py && python telegram_bot.py`

For OAuth, set `OAUTH_REDIRECT_URI=https://your-app.up.railway.app/oauth/callback` and ensure Railway exposes the PORT.

Cost: ~$0-5/month on Railway's hobby plan.

---

# Testing Methodology

## Pre-Test Setup

1. Ensure `.env` is populated with all required variables
2. Have a second Telegram account available (or use a friend's) for non-admin testing
3. Export a calendar file from Google Calendar (Settings > Import & Export > Export) to use as test data
4. Run `python telegram_bot.py` locally and confirm "Bot started" log appears
5. For OAuth tests: set `OAUTH_REDIRECT_URI=http://localhost:8000/oauth/callback` and configure the same URL in GCP Console

## Test Plan

### Test 1: Bot Startup & Admin Auto-Registration

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1.1 | Run `python telegram_bot.py` | Bot starts, logs show "Bot starting in polling mode" |
| 1.2 | Check `users.json` in DATA_DIR | Admin user entry exists with `is_admin: true`, status `ready` |
| 1.3 | (If OAuth configured) Check logs | "Starting OAuth callback server on port 8000" appears |

### Test 2: Admin Commands

| Step | Action | Expected Result |
|------|--------|-----------------|
| 2.1 | Send `/start` to bot | Welcome message with list of commands including `/sync` |
| 2.2 | Send `/status` | Shows admin status as "ready" with "Google Calendar: connected (service account)" |
| 2.3 | Send `/sync` | Fetches last 7 days from Google Calendar, shows summary of new events |
| 2.4 | Send a question like "What did I do yesterday?" | Agent responds with calendar data |
| 2.5 | Send `/new` | Session reset confirmation |
| 2.6 | Send the same question again | Agent responds fresh (no prior context) |

### Test 3: New User Registration (Non-Admin Account)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 3.1 | From a different Telegram account, send `/start` | Welcome message: "Send /register to get started" |
| 3.2 | Send any text message | Rejection: "Please /register first to use this bot." |
| 3.3 | Send `/register` | Success: "You're registered! Here's how to get your calendar data..." |
| 3.4 | Check `users.json` | New user entry with status `registered` |
| 3.5 | Check filesystem | Directory created at `{DATA_DIR}/users/{user_id}/` |

### Test 4: Calendar File Upload

| Step | Action | Expected Result |
|------|--------|-----------------|
| 4.1 | Send a `.ics` file to the bot | Success: "Calendar data received! X events found." |
| 4.2 | Send a `.zip` file (Google export) | Success: "Calendar data received! X events found." |
| 4.3 | Send a random non-calendar file (e.g. .txt) | Rejection: "Please send a .ics or .zip calendar export file." |
| 4.4 | Check user directory | `calendar_raw_full.csv` exists with parsed events |
| 4.5 | Check `users.json` | User status updated to `data_uploaded`, `event_count` populated |

### Test 5: ETL Processing

| Step | Action | Expected Result |
|------|--------|-----------------|
| 5.1 | Send `/process` | Bot acknowledges: "Starting data processing..." |
| 5.2 | Wait for ETL to complete | Bot confirms with ETL results + "Ask me anything about your calendar!" |
| 5.3 | Check user directory | `calendar.db`, `calendar_vectors/`, `taxonomy.json` all exist |
| 5.4 | Check `users.json` | User status updated to `ready` |
| 5.5 | Send `/status` | Shows "ready" with event count |

### Test 6: Non-Admin Querying

| Step | Action | Expected Result |
|------|--------|-----------------|
| 6.1 | Send "What did I do last week?" | Agent responds with data from the user's own calendar |
| 6.2 | Send "Who do I spend the most time with?" | Agent responds using SQL on user's DB |
| 6.3 | Send a vibes question like "When was I happiest?" | Agent uses semantic search on user's vectors |
| 6.4 | Send `/new` then re-ask | Fresh session, no prior context |

### Test 7: User Isolation

| Step | Action | Expected Result |
|------|--------|-----------------|
| 7.1 | From admin account, ask about a person only in admin's calendar | Gets results |
| 7.2 | From non-admin account, ask about that same person | Gets no results (data is isolated) |
| 7.3 | From non-admin, ask about someone in their own calendar | Gets results |

### Test 8: OAuth Sync (Non-Admin)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 8.1 | Non-admin sends `/sync` with no stored token | Bot sends Google OAuth authorization URL |
| 8.2 | Click the OAuth link, complete Google consent | Redirected to callback, see "Success!" HTML page |
| 8.3 | Check Telegram | Bot sends "Google Calendar connected! Initial sync complete — X events imported." |
| 8.4 | Check user directory | `google_token.json`, `calendar.db`, `calendar_vectors/` exist |
| 8.5 | Send `/sync` again | Skips OAuth, syncs directly with stored token |
| 8.6 | Send `/status` | Shows "Google Calendar: connected (OAuth)" |
| 8.7 | Wait 10+ minutes, then try the original OAuth link | "Link expired" error page |
| 8.8 | Unset `GOOGLE_CLIENT_ID`, restart bot, non-admin `/sync` | "Google Calendar sync isn't configured yet." |

### Test 9: Edge Cases

| Step | Action | Expected Result |
|------|--------|-----------------|
| 9.1 | Send `/register` when already registered | "You're already registered!" |
| 9.2 | Send `/process` before uploading data | "No calendar data found. Please upload your .ics or .zip file first." |
| 9.3 | Upload a second calendar file | Overwrites previous CSV, user can re-run `/process` |
| 9.4 | Send a very long message | Bot handles gracefully, splits response if needed |
| 9.5 | Rapid-fire multiple questions | Bot queues and responds to each |

### Test 10: Session & Memory

| Step | Action | Expected Result |
|------|--------|-----------------|
| 10.1 | Ask "How many events do I have?" | Gets a count |
| 10.2 | Follow up with "Break that down by month" | Agent uses prior context to understand "that" |
| 10.3 | Tell the bot "Remember that Sarah is my coworker" | Bot saves memory |
| 10.4 | In a new session (`/new`), ask "What events did I have with my coworker?" | Bot recalls Sarah = coworker from memory |

## Post-Test Verification

- [ ] `users.json` has correct entries for all test users
- [ ] Each user's data directory contains expected files
- [ ] No cross-user data leakage
- [ ] Bot stays running without crashes through all tests
- [ ] OAuth tokens are stored per-user, not shared
- [ ] Logs show no unhandled exceptions
