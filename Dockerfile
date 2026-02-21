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
