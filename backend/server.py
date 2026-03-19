# server.py — ЛексАнализ backend v3 (Railway Production)
# Pipeline: Upload → converter (docx/pdf→txt) → chunker → DB → analyze → DB
#
# New in v3:
#   POST /api/analyze-auto   — upload 1 doc, auto-match from pravo DB
#   POST /api/upload-pravo   — seed DB with pravo.by documents

from __future__ import annotations
import json, logging, os, sys, traceback
from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.converter import ConversionError, convert_to_text
from backend.services.chunker import build_chunks
from backend.services.analyzer import run_analysis
from backend.db.database import (
    init_db, save_document, get_document, list_documents, delete_document,
    save_analysis, get_analysis, list_analyses, search_similar_document,
    _doc_id, _conn,
)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("lexanaliz")

_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
_FRONTEND = os.path.abspath(_FRONTEND)

logger.info(f"Frontend path: {_FRONTEND}")
logger.info(f"Frontend exists: {os.path.exists(_FRONTEND)}")
if os.path.exists(_FRONTEND):
    logger.info(f"Frontend contents: {os.listdir(_FRONTEND)}")
    index_path = os.path.join(_FRONTEND, "index.html")
    if os.path.exists(index_path):
        logger.info(f"✅ index.html found: {os.path.getsize(index_path)} bytes")
    else:
        logger.error(f"❌ index.html NOT FOUND")

app = Flask(__name__, static_folder=_FRONTEND, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
app.config["JSON_AS_ASCII"] = False


@app.after_request
def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File too large. Maximum size is 50MB"}), 413


@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({"error": "Internal server error"}), 500


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def _frontend(path: str):
    fp = os.path.join(_FRONTEND, path)
    if path and os.path.exists(fp):
        return send_from_directory(_FRONTEND, path)
    return send_from_directory(_FRONTEND, "index.html")


@app.get("/health")
def health():
    try:
        with _conn() as con:
            con.execute("SELECT 1").fetchone()
        return jsonify({
            "status": "ok",
            "version": "3.0.0",
            "database": "connected",
            "frontend_path": _FRONTEND,
            "index_exists": os.path.exists(os.path.join(_FRONTEND, "index.html"))
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error", "database": "disconnected", "error": str(e)}), 503


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_upload(f) -> tuple[dict, bytes]:
    """
    Parse an uploaded FileStorage object.
    Returns (parsed_dict, raw_bytes) or raises ValueError / ConversionError.
    """
    if not f or not f.filename:
        raise ValueError("No file provided")

    data = f.read()
    filename = f.filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    if not data:
        raise ValueError("Empty file")

    plain_text = convert_to_text(filename, data)
    paragraphs = [l.strip() for l in plain_text.splitlines() if l.strip()]
    chunks = build_chunks(paragraphs)

    if not paragraphs:
        raise ValueError("No text extracted from document")

    parsed = {
        "filename":   filename,
        "ext":        ext,
        "char_count": len(plain_text),
        "para_count": len(paragraphs),
        "chunk_count": len(chunks),
        "paragraphs": paragraphs,
        "chunks":     chunks,
        "plain_text": plain_text,
    }
    return parsed, data


# ─── POST /api/parse ──────────────────────────────────────────────────────────

@app.route("/api/parse", methods=["POST", "OPTIONS"])
def api_parse():
    """Parse uploaded document (DOCX, PDF, TXT). Mode 2 helper."""
    if request.method == "OPTIONS":
        return jsonify({}), 204

    if "file" not in request.files:
        return jsonify({"error": "No 'file' field"}), 400

    try:
        parsed, data = _parse_upload(request.files["file"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ConversionError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Parse failed: {exc}")
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    source = request.form.get("source", "upload")
    title  = request.form.get("title", "").strip() or None

    try:
        doc_id = save_document(
            parsed["filename"], parsed["ext"], data,
            parsed["plain_text"], parsed["paragraphs"], parsed["chunks"],
            source=source, title=title,
        )
    except Exception as exc:
        logger.warning(f"DB save skipped: {exc}")
        doc_id = _doc_id(data)

    parsed["doc_id"] = doc_id
    logger.info(f"Parsed {parsed['filename']}: {parsed['char_count']} chars, source={source}")
    return jsonify(parsed)


# ─── POST /api/analyze ────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def api_analyze():
    """Mode 2: Compare two pre-parsed documents."""
    if request.method == "OPTIONS":
        return jsonify({}), 204

    body = request.get_json(silent=True) or {}
    old_parsed = body.get("old")
    new_parsed  = body.get("new")
    api_key    = (body.get("apiKey") or "").strip()
    model      = body.get("model") or None

    if not isinstance(old_parsed, dict):
        return jsonify({"error": "Field 'old' required (parsed document)"}), 400
    if not isinstance(new_parsed, dict):
        return jsonify({"error": "Field 'new' required (parsed document)"}), 400
    if not api_key:
        return jsonify({"error": "Field 'apiKey' required"}), 400
    if not old_parsed.get("paragraphs"):
        return jsonify({"error": "'old' document has no text"}), 400
    if not new_parsed.get("paragraphs"):
        return jsonify({"error": "'new' document has no text"}), 400

    if not old_parsed.get("chunks"):
        old_parsed["chunks"] = build_chunks(old_parsed["paragraphs"])
    if not new_parsed.get("chunks"):
        new_parsed["chunks"] = build_chunks(new_parsed["paragraphs"])

    old_doc_id = old_parsed.get("doc_id", "")
    new_doc_id  = new_parsed.get("doc_id", "")

    logger.info(f"Analyze (manual): {old_doc_id[:8]} vs {new_doc_id[:8]}")

    if old_doc_id and new_doc_id:
        cached = get_analysis(old_doc_id, new_doc_id)
        if cached:
            cached["_cached"] = True
            return jsonify(cached)

    try:
        result = run_analysis(
            old_parsed, new_parsed, api_key, model,
            comparison_type="manual",
        )
    except Exception as exc:
        logger.error(f"Analysis error: {exc}\n{traceback.format_exc()}")
        return jsonify({"error": f"Analysis failed: {exc}"}), 500

    if old_doc_id and new_doc_id:
        try:
            aid = save_analysis(old_doc_id, new_doc_id, result)
            result["analysis_id"] = aid
        except Exception as exc:
            logger.warning(f"Analysis DB save failed: {exc}")

    return jsonify({
        "changes":     result["changes"],
        "red_zones":   result["red_zones"],
        "stats":       result["stats"],
        "metadata":    result["metadata"],
        "synthesis":   result.get("synthesis", {}),
        "analysis_id": result.get("analysis_id"),
    })


# ─── POST /api/analyze-auto ───────────────────────────────────────────────────

@app.route("/api/analyze-auto", methods=["POST", "OPTIONS"])
def api_analyze_auto():
    """
    Mode 1: Upload 1 document → find similar in pravo DB → analyze.

    Expects multipart/form-data:
        file    — the new document
        apiKey  — OpenRouter key
        model   — (optional) model override
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204

    api_key = (request.form.get("apiKey") or "").strip()
    model   = (request.form.get("model") or "").strip() or None

    if not api_key:
        return jsonify({"error": "Field 'apiKey' required"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No 'file' field"}), 400

    # 1. Parse uploaded file
    try:
        new_parsed, data = _parse_upload(request.files["file"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ConversionError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Parse failed: {exc}")
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    # 2. Save uploaded doc to DB (as source="upload")
    try:
        new_doc_id = save_document(
            new_parsed["filename"], new_parsed["ext"], data,
            new_parsed["plain_text"], new_parsed["paragraphs"], new_parsed["chunks"],
            source="upload",
        )
        new_parsed["doc_id"] = new_doc_id
    except Exception as exc:
        logger.warning(f"DB save (new doc) failed: {exc}")
        new_doc_id = _doc_id(data)
        new_parsed["doc_id"] = new_doc_id

    # 3. Search for similar document in pravo DB
    logger.info(f"Searching for similar pravo doc for: {new_parsed['filename']}")
    match = search_similar_document(new_parsed["plain_text"], source="pravo")

    if match is None:
        return jsonify({
            "error": "No similar document found in the database. "
                     "Please seed the database with pravo.by documents first "
                     "(use POST /api/upload-pravo)."
        }), 404

    old_parsed = {
        "doc_id":     match["id"],
        "filename":   match["filename"],
        "char_count": match.get("char_count", 0),
        "para_count": len(match.get("paragraphs", [])),
        "chunk_count": len(match.get("chunks", [])),
        "paragraphs": match.get("paragraphs", []),
        "chunks":     match.get("chunks", []),
        "plain_text": match.get("plain_text", ""),
    }

    if not old_parsed["chunks"]:
        old_parsed["chunks"] = build_chunks(old_parsed["paragraphs"])

    logger.info(
        f"Analyze-auto: new={new_parsed['filename']} "
        f"matched={old_parsed['filename']} score={match['score']:.3f}"
    )

    # 4. Check cached analysis
    old_doc_id = old_parsed["doc_id"]
    cached = get_analysis(old_doc_id, new_doc_id)
    if cached:
        cached["_cached"] = True
        cached["match"] = {
            "doc_id":   match["id"],
            "filename": match["filename"],
            "title":    match.get("title") or match["filename"],
            "score":    match["score"],
        }
        return jsonify(cached)

    # 5. Run analysis
    try:
        result = run_analysis(
            old_parsed, new_parsed, api_key, model,
            comparison_type="auto",
        )
    except Exception as exc:
        logger.error(f"Auto-analysis error: {exc}\n{traceback.format_exc()}")
        return jsonify({"error": f"Analysis failed: {exc}"}), 500

    # 6. Save analysis
    try:
        aid = save_analysis(old_doc_id, new_doc_id, result)
        result["analysis_id"] = aid
    except Exception as exc:
        logger.warning(f"Analysis DB save failed: {exc}")

    return jsonify({
        "match": {
            "doc_id":   match["id"],
            "filename": match["filename"],
            "title":    match.get("title") or match["filename"],
            "score":    match["score"],
        },
        "changes":     result["changes"],
        "red_zones":   result["red_zones"],
        "stats":       result["stats"],
        "metadata":    result["metadata"],
        "synthesis":   result.get("synthesis", {}),
        "analysis_id": result.get("analysis_id"),
    })


# ─── POST /api/upload-pravo ───────────────────────────────────────────────────

@app.route("/api/upload-pravo", methods=["POST", "OPTIONS"])
def api_upload_pravo():
    """
    Seed the DB with a pravo.by document.
    Expects multipart/form-data:
        file   — document file
        title  — (optional) human-readable document title
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204

    if "file" not in request.files:
        return jsonify({"error": "No 'file' field"}), 400

    title = (request.form.get("title") or "").strip() or None

    try:
        parsed, data = _parse_upload(request.files["file"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ConversionError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Upload-pravo parse failed: {exc}")
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    try:
        doc_id = save_document(
            parsed["filename"], parsed["ext"], data,
            parsed["plain_text"], parsed["paragraphs"], parsed["chunks"],
            source="pravo",
            title=title or parsed["filename"],
        )
    except Exception as exc:
        logger.error(f"Upload-pravo DB save failed: {exc}")
        return jsonify({"error": f"DB save failed: {exc}"}), 500

    logger.info(f"Uploaded pravo doc: {parsed['filename']} id={doc_id[:8]}")
    return jsonify({
        "doc_id":      doc_id,
        "filename":    parsed["filename"],
        "title":       title or parsed["filename"],
        "source":      "pravo",
        "char_count":  parsed["char_count"],
        "para_count":  parsed["para_count"],
        "chunk_count": parsed["chunk_count"],
    })


# ─── GET /api/pravo-docs ─────────────────────────────────────────────────────

@app.get("/api/pravo-docs")
def api_list_pravo_docs():
    """List all pravo.by documents in the DB."""
    try:
        with _conn() as con:
            rows = con.execute(
                """SELECT id, filename, title, char_count, para_count, created_at
                   FROM documents
                   WHERE source = 'pravo'
                   ORDER BY created_at DESC
                   LIMIT 200"""
            ).fetchall()
        return jsonify({"documents": [dict(r) for r in rows], "count": len(rows)})
    except Exception as exc:
        logger.error(f"List pravo-docs failed: {exc}")
        return jsonify({"error": str(exc)}), 500


# ─── Documents CRUD ───────────────────────────────────────────────────────────

@app.get("/api/documents")
def api_list_docs():
    try:
        docs = list_documents(100)
        return jsonify({"documents": docs, "count": len(docs)})
    except Exception as exc:
        logger.error(f"List documents failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/documents/<doc_id>")
def api_get_doc(doc_id: str):
    doc = get_document(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    doc.pop("plain_text", None)
    return jsonify(doc)


@app.route("/api/documents/<doc_id>", methods=["DELETE", "OPTIONS"])
def api_del_doc(doc_id: str):
    if request.method == "OPTIONS":
        return jsonify({}), 204
    if not delete_document(doc_id):
        return jsonify({"error": "Document not found"}), 404
    logger.info(f"Deleted document {doc_id[:8]}")
    return jsonify({"deleted": doc_id})


# ─── Analyses ─────────────────────────────────────────────────────────────────

@app.get("/api/analyses")
def api_list_analyses():
    try:
        return jsonify({"analyses": list_analyses(100)})
    except Exception as exc:
        logger.error(f"List analyses failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/analyses/<analysis_id>")
def api_get_analysis(analysis_id: str):
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM analyses WHERE id=?", (analysis_id,)
        ).fetchone()
    if not row:
        return jsonify({"error": "Analysis not found"}), 404
    d = dict(row)
    for f in ("changes", "red_zones", "stats", "metadata", "synthesis"):
        d[f] = json.loads(d[f]) if d.get(f) else {}
    return jsonify(d)


# ─── Entry ────────────────────────────────────────────────────────────────────

def create_app() -> Flask:
    init_db()
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    init_db()
    logger.info(f"🚀 Starting ЛексАнализ v3 on http://0.0.0.0:{port}")
    logger.info(f"📁 Database: {os.environ.get('DB_PATH', '/app/data/lexanaliz.db')}")
    app.run(host="0.0.0.0", port=port, debug=False)