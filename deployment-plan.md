# Deploy RTT-Reader Telegram Bot to Railway

## Context

The Telegram bot (`telegram_bot.py`) currently runs locally via `python telegram_bot.py` with long-polling. To use it 24/7, we need to deploy it to the cloud. Railway was chosen as the platform — it supports always-on workers, persistent volumes, and has simple git-push or CLI deploys. We'll deploy only the Telegram bot (not the FastAPI web UI).

## What Needs to Change

### 1. Add `DATA_DIR` environment variable support to file path constants

All data files (SQLite DB, LanceDB vectors, memory, caches) need to live on a persistent volume in production (`/data`), while still working as-is locally.

**Pattern**: Replace hardcoded `os.path.dirname(__file__)` and relative paths with `os.getenv("DATA_DIR", os.path.dirname(__file__))`.

**`db.py`** (lines 18-21):
```python
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DB_FILE = os.path.join(_DATA_DIR, "calendar.db")
TAXONOMY_FILE = os.path.join(_DATA_DIR, "taxonomy.json")
VECTOR_DIR = os.path.join(_DATA_DIR, "calendar_vectors")
MEMORY_FILE = os.path.join(_DATA_DIR, "memory.json")
```

**`sync.py`** (lines 19-23):
```python
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DB_FILE = os.path.join(_DATA_DIR, "calendar.db")
CSV_FILE = os.path.join(_DATA_DIR, "calendar_raw_full.csv")
TAXONOMY_FILE = os.path.join(_DATA_DIR, "taxonomy.json")
ENRICHMENT_CACHE = os.path.join(_DATA_DIR, "enrichment_cache.json")
DISCOVERY_CACHE = os.path.join(_DATA_DIR, "discovery_cache.json")
```

**`etl.py`** (lines 29-34 — currently uses bare relative paths):
```python
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
CSV_FILE = os.path.join(_DATA_DIR, "calendar_raw_full.csv")
DB_FILE = os.path.join(_DATA_DIR, "calendar.db")
DISCOVERY_CACHE = os.path.join(_DATA_DIR, "discovery_cache.json")
TAXONOMY_FILE = os.path.join(_DATA_DIR, "taxonomy.json")
ENRICHMENT_CACHE = os.path.join(_DATA_DIR, "enrichment_cache.json")
VECTOR_DIR = os.path.join(_DATA_DIR, "calendar_vectors")
```

### 2. Pin `requirements.txt` and add missing dependencies

Current file has unpinned deps and is missing the `google-*` packages needed for `/sync`. Generate a pinned version from the working venv via `pip freeze`, filtered to the needed packages.

### 3. Create `Dockerfile`

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py api.py db.py etl.py sync.py telegram_bot.py \
     data_extract.py data-extract.py ./
COPY taxonomy.json ./
COPY static/ ./static/

CMD ["python", "telegram_bot.py"]
```

- `python:3.11-slim`: Needed for LanceDB/PyArrow compatibility
- `build-essential`: For any native extension compilation
- Only app code is copied — data files live on the persistent volume
- No `.env` or credentials baked into the image

### 4. Create `.dockerignore`

Exclude venv, data files, credentials, caches, and dev artifacts from the Docker build context.

### 5. Create `railway.toml`

Railway auto-detects Dockerfiles, but we can add a config for explicit settings:
```toml
[build]
dockerfilePath = "Dockerfile"

[deploy]
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 10
```

## Deployment Steps (after code changes)

1. **Push code to GitHub** (Railway deploys from git)
2. **Create Railway project** at railway.app → "New Project" → "Deploy from GitHub repo"
3. **Add a persistent volume** in Railway dashboard: mount path `/data`, 1GB
4. **Set environment variables** in Railway dashboard:
   - `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`
   - `CALENDAR_ID`, `YOUR_TIMEZONE=America/Los_Angeles`
   - `DATA_DIR=/data`
   - `SERVICE_ACCOUNT_FILE=/data/service_account.json`
5. **Seed the data volume** — Upload existing data files to `/data` via `railway shell`:
   ```bash
   # From local machine, open a shell on the Railway container:
   railway shell
   # Then transfer files (or tar + upload via temporary method)
   ```
6. **Upload service account JSON** to `/data/service_account.json` on the volume
7. **Deploy** — Railway auto-deploys on git push

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `db.py` | Modify lines 18-21 | Add `DATA_DIR` support |
| `sync.py` | Modify lines 19-23 | Add `DATA_DIR` support |
| `etl.py` | Modify lines 29-34 | Add `DATA_DIR` support |
| `requirements.txt` | Rewrite | Pin versions, add google-* deps |
| `Dockerfile` | Create | Container definition |
| `.dockerignore` | Create | Exclude data/secrets from build |
| `railway.toml` | Create | Railway deploy config |

## Cost Estimate

Railway pricing is usage-based:
- **Hobby plan**: $5/month credit included
- This bot is mostly idle (waiting for Telegram updates) with brief CPU spikes for LLM API calls
- Expected usage: well within the $5 credit → **~$0-5/month**
- Persistent volume: $0.25/GB/month → negligible

## Verification

1. **Local Docker test**: `docker build -t rtt-reader . && docker run --env-file .env -v $(pwd):/data -e DATA_DIR=/data rtt-reader` — verify bot starts and responds
2. **After Railway deploy**: Send a message to the bot on Telegram, verify response
3. **Test /sync**: Run `/sync` in Telegram, verify it fetches and enriches new events
4. **Test /new**: Run `/new`, verify session resets
5. **Check Railway logs**: Verify no errors, bot is polling

## Why Not Other Platforms?

- **Vercel**: Serverless/request-based only. Cannot run a long-polling bot process. No persistent disk storage.
- **Fly.io**: Great alternative (native worker support, persistent volumes, free tier). Good fallback if Railway doesn't work out.
- **Google Cloud Run**: Request-based. Always-on requires `--min-instances=1` which costs ~$10+/mo.
- **Small VPS (DigitalOcean/Lightsail)**: Works but more maintenance (OS updates, security patches). $4-6/mo fixed.
