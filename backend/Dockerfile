FROM python:3.11-slim

# System deps for psycopg2, reportlab, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir psycopg2-binary

COPY . .

# Default: run API server (override CMD in docker-compose for worker)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
