FROM python:3.12-slim

# System dependencies for PDF processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libxml2 \
    libxslt1.1 \
    tree \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create necessary directories
RUN mkdir -p /app/data

# ═══════════════════════════════════════════════════════════════════════════
# COMPREHENSIVE DEBUG OUTPUT
# ═══════════════════════════════════════════════════════════════════════════

RUN echo "════════════════════════════════════════════════════════════════" && \
    echo "🔍 DOCKER BUILD DEBUG OUTPUT" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "" && \
    echo "📂 FULL DIRECTORY TREE:" && \
    tree -L 4 /app || ls -laR /app && \
    echo "" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "📁 /app contents:" && \
    ls -lah /app && \
    echo "" && \
    echo "────────────────────────────────────────────────────────────────" && \
    echo "📁 /app/backend/ contents:" && \
    ls -lah /app/backend/ && \
    echo "" && \
    echo "────────────────────────────────────────────────────────────────" && \
    echo "📁 /app/backend/services/ contents:" && \
    ls -lah /app/backend/services/ && \
    echo "" && \
    echo "────────────────────────────────────────────────────────────────" && \
    echo "📁 /app/backend/db/ contents:" && \
    ls -lah /app/backend/db/ && \
    echo "" && \
    echo "────────────────────────────────────────────────────────────────" && \
    echo "📁 /app/frontend/ contents:" && \
    ls -lah /app/frontend/ && \
    echo "" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "🔍 CRITICAL FILES CHECK:" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "" && \
    echo "✓ Checking /app/backend/__init__.py:" && \
    (test -f /app/backend/__init__.py && echo "  ✅ EXISTS ($(wc -c < /app/backend/__init__.py) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "✓ Checking /app/backend/server.py:" && \
    (test -f /app/backend/server.py && echo "  ✅ EXISTS ($(wc -c < /app/backend/server.py) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "✓ Checking /app/backend/services/__init__.py:" && \
    (test -f /app/backend/services/__init__.py && echo "  ✅ EXISTS ($(wc -c < /app/backend/services/__init__.py) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "✓ Checking /app/backend/services/analyzer.py:" && \
    (test -f /app/backend/services/analyzer.py && echo "  ✅ EXISTS ($(wc -c < /app/backend/services/analyzer.py) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "✓ Checking /app/backend/db/__init__.py:" && \
    (test -f /app/backend/db/__init__.py && echo "  ✅ EXISTS ($(wc -c < /app/backend/db/__init__.py) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "✓ Checking /app/backend/db/database.py:" && \
    (test -f /app/backend/db/database.py && echo "  ✅ EXISTS ($(wc -c < /app/backend/db/database.py) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "✓ Checking /app/frontend/index.html:" && \
    (test -f /app/frontend/index.html && echo "  ✅ EXISTS ($(wc -c < /app/frontend/index.html) bytes)" || echo "  ❌ MISSING") && \
    echo "" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "📊 FILE SIZES:" && \
    echo "════════════════════════════════════════════════════════════════" && \
    du -sh /app/backend /app/frontend /app 2>/dev/null || echo "  (du command failed)" && \
    echo "" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "🐍 PYTHON IMPORT TEST:" && \
    echo "════════════════════════════════════════════════════════════════" && \
    cd /app && python3 -c "import sys; sys.path.insert(0, '/app'); print('sys.path:', sys.path); from backend.db.database import init_db; print('✅ backend.db.database import OK'); from backend.services.analyzer import run_analysis; print('✅ backend.services.analyzer import OK')" && \
    echo "" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "✅ BUILD VERIFICATION COMPLETE" && \
    echo "════════════════════════════════════════════════════════════════"

# Environment variables
ENV DB_PATH=/app/data/lexanaliz.db
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Use gunicorn for production with extended timeouts
CMD echo "════════════════════════════════════════════════════════════════" && \
    echo "🚀 STARTING APPLICATION" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "Environment:" && \
    echo "  PORT=$PORT" && \
    echo "  DB_PATH=$DB_PATH" && \
    echo "  PYTHONUNBUFFERED=$PYTHONUNBUFFERED" && \
    echo "" && \
    echo "Working directory: $(pwd)" && \
    echo "Contents:" && \
    ls -la && \
    echo "" && \
    echo "Frontend check:" && \
    ls -la /app/frontend/ && \
    echo "" && \
    echo "════════════════════════════════════════════════════════════════" && \
    echo "Starting gunicorn..." && \
    echo "════════════════════════════════════════════════════════════════" && \
    gunicorn --bind 0.0.0.0:$PORT \
        --workers 1 \
        --threads 2 \
        --timeout 600 \
        --graceful-timeout 60 \
        --keep-alive 5 \
        --log-level debug \
        --access-logfile - \
        --error-logfile - \
        --access-logformat '%({X-Forwarded-For}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s' \
        "backend.server:create_app()"
