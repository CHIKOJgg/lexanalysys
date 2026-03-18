# db/database.py
# SQLite storage for uploaded documents and analysis results.
#
# Tables:
#   documents  — uploaded files (raw bytes + extracted text + metadata)
#   analyses   — analysis results linked to document pairs

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# DB_PATH env var lets Docker mount it to a persistent volume
_DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "lexanaliz.db")))


# ── Connection ────────────────────────────────────────────────────────────────


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(str(_DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,      -- sha256 of file bytes
    filename    TEXT NOT NULL,
    ext         TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    char_count  INTEGER NOT NULL,
    para_count  INTEGER NOT NULL,
    plain_text  TEXT NOT NULL,         -- converted plain text
    paragraphs  TEXT NOT NULL,         -- JSON array
    chunks      TEXT NOT NULL,         -- JSON array
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id          TEXT PRIMARY KEY,      -- sha256(old_id + new_id)
    old_doc_id  TEXT NOT NULL REFERENCES documents(id),
    new_doc_id  TEXT NOT NULL REFERENCES documents(id),
    model_used  TEXT,
    changes     TEXT NOT NULL,         -- JSON
    red_zones   TEXT NOT NULL,         -- JSON
    stats       TEXT NOT NULL,         -- JSON
    metadata    TEXT NOT NULL,         -- JSON
    synthesis   TEXT NOT NULL,         -- JSON
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_docs ON analyses(old_doc_id, new_doc_id);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at);
"""


def init_db() -> None:
    """Create tables if they don't exist."""
    with _conn() as con:
        con.executescript(_SCHEMA)
    logger.info("DB initialised at %s", _DB_PATH)


# ── Documents ─────────────────────────────────────────────────────────────────


def _doc_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_document(
    filename: str,
    ext: str,
    data: bytes,
    plain_text: str,
    paragraphs: list[str],
    chunks: list[dict],
) -> str:
    """
    Upsert document. Returns document id (sha256 of raw bytes).
    Idempotent: same file uploaded again returns same id without re-writing.
    """
    doc_id = _doc_id(data)

    with _conn() as con:
        existing = con.execute(
            "SELECT id FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()

        if existing:
            logger.info("Document already stored: %s", doc_id[:12])
            return doc_id

        con.execute(
            """
            INSERT INTO documents
                (id, filename, ext, size_bytes, char_count, para_count,
                 plain_text, paragraphs, chunks, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                filename,
                ext,
                len(data),
                len(plain_text),
                len(paragraphs),
                plain_text,
                json.dumps(paragraphs, ensure_ascii=False),
                json.dumps(chunks, ensure_ascii=False),
                time.time(),
            ),
        )

    logger.info("Document saved: %s (%s, %d chars)", doc_id[:12], filename, len(plain_text))
    return doc_id


def get_document(doc_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["paragraphs"] = json.loads(d["paragraphs"])
    d["chunks"] = json.loads(d["chunks"])
    return d


def list_documents(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, filename, ext, size_bytes, char_count, para_count, created_at "
            "FROM documents ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_document(doc_id: str) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return cur.rowcount > 0


# ── Analyses ──────────────────────────────────────────────────────────────────


def _analysis_id(old_id: str, new_id: str) -> str:
    return hashlib.sha256(f"{old_id}|{new_id}".encode()).hexdigest()


def save_analysis(
    old_doc_id: str,
    new_doc_id: str,
    result: dict,
) -> str:
    """Save analysis result. Returns analysis id."""
    analysis_id = _analysis_id(old_doc_id, new_doc_id)
    meta = result.get("metadata", {})

    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO analyses
                (id, old_doc_id, new_doc_id, model_used,
                 changes, red_zones, stats, metadata, synthesis, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id,
                old_doc_id,
                new_doc_id,
                meta.get("model_used"),
                json.dumps(result.get("changes", []),   ensure_ascii=False),
                json.dumps(result.get("red_zones", []), ensure_ascii=False),
                json.dumps(result.get("stats", {}),     ensure_ascii=False),
                json.dumps(meta,                        ensure_ascii=False),
                json.dumps(result.get("synthesis", {}), ensure_ascii=False),
                time.time(),
            ),
        )
    logger.info("Analysis saved: %s", analysis_id[:12])
    return analysis_id


def get_analysis(old_doc_id: str, new_doc_id: str) -> dict | None:
    analysis_id = _analysis_id(old_doc_id, new_doc_id)
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for field in ("changes", "red_zones", "stats", "metadata", "synthesis"):
        d[field] = json.loads(d[field])
    return d


def list_analyses(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT a.id, a.old_doc_id, a.new_doc_id, a.model_used, a.created_at,
                   o.filename AS old_filename, n.filename AS new_filename,
                   a.stats
            FROM analyses a
            JOIN documents o ON o.id = a.old_doc_id
            JOIN documents n ON n.id = a.new_doc_id
            ORDER BY a.created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["stats"] = json.loads(d["stats"])
        result.append(d)
    return result