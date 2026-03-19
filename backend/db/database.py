"""
database.py — SQLite persistence layer for ЛексАнализ

Changes v2:
- Added source ("upload" | "pravo") and title columns
- Added indexes on source and title
- Added search_similar_document() via SequenceMatcher
- Auto-migration: adds new columns if DB already exists
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "lexanaliz.db"),
)
DB_PATH = os.path.normpath(DB_PATH)

# Minimum similarity score to accept a match (0.0–1.0)
SEARCH_MIN_SCORE = 0.05


# ─── Connection ───────────────────────────────────────────────────────────────

@contextmanager
def _conn():
    """Thread-safe SQLite connection as context manager."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ─── ID ───────────────────────────────────────────────────────────────────────

def _doc_id(data: bytes) -> str:
    """Stable SHA-256 content hash used as document PK."""
    return hashlib.sha256(data).hexdigest()


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables and indexes. Safe to call multiple times."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id          TEXT PRIMARY KEY,
                filename    TEXT NOT NULL,
                ext         TEXT,
                char_count  INTEGER,
                para_count  INTEGER,
                chunk_count INTEGER,
                plain_text  TEXT,
                paragraphs  TEXT,
                chunks      TEXT,
                source      TEXT DEFAULT 'upload',
                title       TEXT,
                created_at  INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_documents_source
                ON documents(source);

            CREATE INDEX IF NOT EXISTS idx_documents_title
                ON documents(title);

            CREATE TABLE IF NOT EXISTS analyses (
                id           TEXT PRIMARY KEY,
                old_doc_id   TEXT,
                new_doc_id   TEXT,
                old_filename TEXT,
                new_filename TEXT,
                changes      TEXT,
                red_zones    TEXT,
                stats        TEXT,
                metadata     TEXT,
                synthesis    TEXT,
                model_used   TEXT,
                created_at   INTEGER
            );
        """)

        # ── Migration: add new columns to existing DB ─────────────────────────
        for col, definition in [
            ("source", "TEXT DEFAULT 'upload'"),
            ("title",  "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE documents ADD COLUMN {col} {definition}")
                logger.info("Migration: added column documents.%s", col)
            except sqlite3.OperationalError:
                pass  # column already exists — OK

    logger.info("DB ready: %s", DB_PATH)


# ─── Documents CRUD ───────────────────────────────────────────────────────────

def save_document(
    filename: str,
    ext: str,
    data: bytes,
    plain_text: str,
    paragraphs: list,
    chunks: list,
    source: str = "upload",
    title: str | None = None,
) -> str:
    """
    Insert or replace document. Returns doc_id (SHA-256 of raw bytes).
    source: "upload" | "pravo"
    title:  human-readable document title (optional, falls back to filename)
    """
    doc_id = _doc_id(data)
    inferred_title = title or filename

    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO documents
              (id, filename, ext, char_count, para_count, chunk_count,
               plain_text, paragraphs, chunks, source, title, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                doc_id,
                filename,
                ext,
                len(plain_text),
                len(paragraphs),
                len(chunks),
                plain_text,
                json.dumps(paragraphs, ensure_ascii=False),
                json.dumps(chunks,     ensure_ascii=False),
                source,
                inferred_title,
                int(time.time()),
            ),
        )
    logger.info("Saved doc %s source=%s title=%s", doc_id[:8], source, inferred_title)
    return doc_id


def get_document(doc_id: str) -> dict | None:
    """Return document dict or None."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for f in ("paragraphs", "chunks"):
        try:
            d[f] = json.loads(d[f]) if d[f] else []
        except Exception:
            d[f] = []
    return d


def list_documents(limit: int = 100) -> list[dict]:
    """List documents ordered by creation time descending."""
    with _conn() as con:
        rows = con.execute(
            """SELECT id, filename, ext, char_count, para_count, chunk_count,
                      source, title, created_at
               FROM documents
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_document(doc_id: str) -> bool:
    """Delete document. Returns True if found and deleted."""
    with _conn() as con:
        cur = con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    return cur.rowcount > 0


# ─── Search ───────────────────────────────────────────────────────────────────

def search_similar_document(
    query_text: str,
    source: str = "pravo",
    top_k: int = 5,
) -> dict | None:
    """
    Find the most similar document in the DB filtered by source.

    Strategy (MVP, no embeddings):
      1. Pre-filter by keyword LIKE on plain_text (fast)
      2. Score surviving candidates with SequenceMatcher on first 2000 chars
      3. Return the best match above SEARCH_MIN_SCORE, or None

    Returns a dict with keys: id, filename, title, source, score, plain_text,
    paragraphs, chunks — ready to be used as old_parsed in run_analysis().
    """
    if not query_text or not query_text.strip():
        return None

    # Extract a few meaningful keywords from the query (first 1000 chars)
    sample = query_text[:1000]
    # Split to words ≥ 4 chars, take first 5 as keyword filters
    words = [w for w in sample.split() if len(w) >= 4][:5]

    with _conn() as con:
        # ── Stage 1: LIKE keyword pre-filter ─────────────────────────────────
        if words:
            placeholders = " OR ".join(
                ["plain_text LIKE ?"] * len(words)
            )
            sql = f"""
                SELECT id, filename, title, source, plain_text, paragraphs, chunks, char_count
                FROM documents
                WHERE source = ?
                  AND ({placeholders})
                ORDER BY created_at DESC
                LIMIT 50
            """
            params = [source] + [f"%{w}%" for w in words]
            rows = con.execute(sql, params).fetchall()
        else:
            rows = []

        # ── Fallback: no keyword hits → grab all pravo docs ───────────────────
        if not rows:
            rows = con.execute(
                """SELECT id, filename, title, source, plain_text, paragraphs, chunks, char_count
                   FROM documents
                   WHERE source = ?
                   ORDER BY created_at DESC
                   LIMIT 50""",
                (source,),
            ).fetchall()

    if not rows:
        logger.info("search_similar_document: no pravo docs in DB")
        return None

    # ── Stage 2: SequenceMatcher scoring ─────────────────────────────────────
    query_sample = query_text[:2000]
    best_score = -1.0
    best_row: sqlite3.Row | None = None

    for row in rows:
        doc_text = (row["plain_text"] or "")[:2000]
        score = SequenceMatcher(None, query_sample, doc_text).ratio()
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None or best_score < SEARCH_MIN_SCORE:
        logger.info(
            "search_similar_document: best_score=%.3f below threshold=%.2f",
            best_score, SEARCH_MIN_SCORE,
        )
        return None

    d = dict(best_row)
    for f in ("paragraphs", "chunks"):
        try:
            d[f] = json.loads(d[f]) if d[f] else []
        except Exception:
            d[f] = []
    d["score"] = round(best_score, 4)
    logger.info(
        "search_similar_document: matched %s score=%.3f",
        d["filename"], best_score,
    )
    return d


# ─── Analyses CRUD ────────────────────────────────────────────────────────────

def save_analysis(
    old_doc_id: str,
    new_doc_id: str,
    result: dict,
) -> str:
    """Persist analysis result. Returns analysis_id."""
    aid = hashlib.sha256(
        f"{old_doc_id}:{new_doc_id}:{time.time()}".encode()
    ).hexdigest()

    meta = result.get("metadata", {})
    stats = result.get("stats", {})

    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO analyses
              (id, old_doc_id, new_doc_id, old_filename, new_filename,
               changes, red_zones, stats, metadata, synthesis,
               model_used, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                aid,
                old_doc_id,
                new_doc_id,
                meta.get("old_file", ""),
                meta.get("new_file", ""),
                json.dumps(result.get("changes",   []), ensure_ascii=False),
                json.dumps(result.get("red_zones", []), ensure_ascii=False),
                json.dumps(stats,                      ensure_ascii=False),
                json.dumps(meta,                       ensure_ascii=False),
                json.dumps(result.get("synthesis", {}), ensure_ascii=False),
                meta.get("model_used", ""),
                int(time.time()),
            ),
        )
    logger.info("Saved analysis %s", aid[:8])
    return aid


def get_analysis(old_doc_id: str, new_doc_id: str) -> dict | None:
    """Return cached analysis for this doc pair, or None."""
    with _conn() as con:
        row = con.execute(
            """SELECT * FROM analyses
               WHERE old_doc_id=? AND new_doc_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (old_doc_id, new_doc_id),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for f in ("changes", "red_zones", "stats", "metadata", "synthesis"):
        try:
            d[f] = json.loads(d[f]) if d[f] else {}
        except Exception:
            d[f] = {}
    return d


def list_analyses(limit: int = 100) -> list[dict]:
    """List analyses ordered by creation time descending."""
    with _conn() as con:
        rows = con.execute(
            """SELECT id, old_doc_id, new_doc_id, old_filename, new_filename,
                      stats, model_used, created_at
               FROM analyses
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["stats"] = json.loads(d["stats"]) if d["stats"] else {}
        except Exception:
            d["stats"] = {}
        result.append(d)
    return result