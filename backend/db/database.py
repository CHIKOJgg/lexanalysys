# database.py — SQLite persistence for documents and analyses

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/app/data/lexanaliz.db")


def _doc_id(data: bytes) -> str:
    """Generate unique document ID from file content."""
    return sha256(data).hexdigest()


@contextmanager
def _conn():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize database schema."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with _conn() as con:
        # Documents table
        con.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                ext TEXT NOT NULL,
                created_at TEXT NOT NULL,
                char_count INTEGER NOT NULL,
                para_count INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL,
                file_data BLOB,
                plain_text TEXT,
                paragraphs TEXT,
                chunks TEXT
            )
        """)

        # Analyses table
        con.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                old_doc_id TEXT NOT NULL,
                new_doc_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                changes TEXT NOT NULL,
                red_zones TEXT NOT NULL,
                stats TEXT NOT NULL,
                metadata TEXT NOT NULL,
                synthesis TEXT NOT NULL,
                FOREIGN KEY (old_doc_id) REFERENCES documents(id),
                FOREIGN KEY (new_doc_id) REFERENCES documents(id)
            )
        """)

        # Indexes
        con.execute("CREATE INDEX IF NOT EXISTS idx_docs_created ON documents(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_analyses_docs ON analyses(old_doc_id, new_doc_id)")

    logger.info(f"Database initialized at {DB_PATH}")


def save_document(
        filename: str,
        ext: str,
        file_data: bytes,
        plain_text: str,
        paragraphs: list[str],
        chunks: list[dict],
) -> str:
    """Save document to database. Returns doc_id."""
    doc_id = _doc_id(file_data)

    with _conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO documents
            (id, filename, ext, created_at, char_count, para_count, chunk_count,
             file_data, plain_text, paragraphs, chunks)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_id,
            filename,
            ext,
            datetime.utcnow().isoformat(),
            len(plain_text),
            len(paragraphs),
            len(chunks),
            file_data,
            plain_text,
            json.dumps(paragraphs, ensure_ascii=False),
            json.dumps(chunks, ensure_ascii=False),
        ))

    logger.info(f"Saved document {doc_id[:8]} ({filename})")
    return doc_id


def get_document(doc_id: str) -> dict | None:
    """Retrieve document by ID."""
    with _conn() as con:
        row = con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return None

        d = dict(row)
        d["paragraphs"] = json.loads(d["paragraphs"])
        d["chunks"] = json.loads(d["chunks"])
        d.pop("file_data", None)  # Don't return binary data
        return d


def list_documents(limit: int = 100) -> list[dict]:
    """List recent documents."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, filename, ext, created_at, char_count, para_count, chunk_count
            FROM documents
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]


def delete_document(doc_id: str) -> bool:
    """Delete document by ID. Returns True if deleted."""
    with _conn() as con:
        cur = con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return cur.rowcount > 0


def save_analysis(old_doc_id: str, new_doc_id: str, result: dict) -> str:
    """Save analysis result. Returns analysis_id."""
    analysis_id = sha256(f"{old_doc_id}:{new_doc_id}".encode()).hexdigest()

    with _conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO analyses
            (id, old_doc_id, new_doc_id, created_at, changes, red_zones, stats, metadata, synthesis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id,
            old_doc_id,
            new_doc_id,
            datetime.utcnow().isoformat(),
            json.dumps(result.get("changes", []), ensure_ascii=False),
            json.dumps(result.get("red_zones", []), ensure_ascii=False),
            json.dumps(result.get("stats", {}), ensure_ascii=False),
            json.dumps(result.get("metadata", {}), ensure_ascii=False),
            json.dumps(result.get("synthesis", {}), ensure_ascii=False),
        ))

    logger.info(f"Saved analysis {analysis_id[:8]}")
    return analysis_id


def get_analysis(old_doc_id: str, new_doc_id: str) -> dict | None:
    """Get cached analysis for document pair."""
    analysis_id = sha256(f"{old_doc_id}:{new_doc_id}".encode()).hexdigest()

    with _conn() as con:
        row = con.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        if not row:
            return None

        d = dict(row)
        d["changes"] = json.loads(d["changes"])
        d["red_zones"] = json.loads(d["red_zones"])
        d["stats"] = json.loads(d["stats"])
        d["metadata"] = json.loads(d["metadata"])
        d["synthesis"] = json.loads(d["synthesis"])
        return d


def list_analyses(limit: int = 100) -> list[dict]:
    """List recent analyses."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, old_doc_id, new_doc_id, created_at
            FROM analyses
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]