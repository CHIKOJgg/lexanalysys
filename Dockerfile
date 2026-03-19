FROM python:3.12-slim

# System dependencies for PDF processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create necessary directories
RUN mkdir -p /app/data /app/backend/db

# Environment variables (Railway will override PORT)
ENV DB_PATH=/app/data/lexanaliz.db
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Use gunicorn for production
# Замени последнюю строку на:
CMD gunicorn --bind 0.0.0.0:$PORT \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "backend.server:create_app()"