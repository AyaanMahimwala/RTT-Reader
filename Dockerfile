FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py api.py db.py etl.py sync.py telegram_bot.py \
     data_extract.py data-extract.py seed.py ./
COPY taxonomy.json ./
COPY static/ ./static/

# Temporary: bundle data for initial volume seed
COPY calendar.db calendar_raw_full.csv enrichment_cache.json \
     discovery_cache.json memory.json ./seed_data/
COPY rtt-llm-4bb764de12c7.json ./seed_data/service_account.json
COPY taxonomy.json ./seed_data/
COPY calendar_vectors/ ./seed_data/calendar_vectors/

CMD ["sh", "-c", "python seed.py && python telegram_bot.py"]
