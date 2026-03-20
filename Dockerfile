FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libxml2 \
    libxslt1.1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY seed_pravo.py ./seed_pravo.py

# Create necessary directories
RUN mkdir -p /app/data /app/backend/db /app/backend/scraper

# Environment
ENV DB_PATH=/app/data/lexanaliz.db
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# gunicorn: 1 worker (SQLite), 4 threads (parallel requests OK with WAL)
# timeout 600s for long LLM + scraping calls
CMD gunicorn --bind 0.0.0.0:$PORT \
    --workers 1 \
    --threads 4 \
    --timeout 600 \
    --graceful-timeout 60 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    "backend.server:create_app()"