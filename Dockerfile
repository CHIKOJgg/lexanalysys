FROM python:3.12-slim

# System deps for pdfplumber (needs poppler) and python-docx
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# DB volume mount point
RUN mkdir -p /app/backend/db /app/data

# DB file goes to /app/data so it persists via volume
ENV DB_PATH=/app/data/lexanaliz.db
ENV PORT=8000

EXPOSE 8000

CMD ["python", "backend/server.py"]