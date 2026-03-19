FROM python:3.12-slim

# System dependencies for PDF processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ONLY backend (frontend is embedded in server.py)
COPY backend/ ./backend/

# Create data directory
RUN mkdir -p /app/data

# Verify structure
RUN echo "========================================" && \
    echo "Backend structure:" && \
    ls -lh /app/backend/ && \
    echo "Services:" && \
    ls -lh /app/backend/services/ && \
    echo "DB:" && \
    ls -lh /app/backend/db/ && \
    echo "========================================"

# Environment variables
ENV DB_PATH=/app/data/lexanaliz.db
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Use gunicorn for production
CMD gunicorn --bind 0.0.0.0:$PORT \
    --workers 1 \
    --threads 2 \
    --timeout 600 \
    --graceful-timeout 60 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    "backend.server:create_app()"
