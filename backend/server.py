# server.py — ЛексАнализ backend v2 (Production Ready)
# Pipeline: Upload → converter (docx/pdf→txt) → chunker → DB → analyze → DB

from __future__ import annotations
import json, logging, os, sys, traceback
from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.converter import ConversionError, convert_to_text
from backend.services.chunker import build_chunks
from backend.services.analyzer import run_analysis
from backend.db.database import (
    init_db, save_document, get_document, list_documents, delete_document,
    save_analysis, get_analysis, list_analyses, _doc_id, _conn,
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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
app.config["JSON_AS_ASCII"] = False  # Support Cyrillic in JSON responses


@app.after_request
def _cors(resp: Response) -> Response:
    """CORS headers for all responses."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle file too large errors."""
    return jsonify({"error": "File too large. Maximum size is 50MB"}), 413


@app.errorhandler(500)
def internal_error(error):
    """Handle internal server errors."""
    logger.error(f"Internal error: {error}")
    return jsonify({"error": "Internal server error"}), 500


@app.route("/")
def index():
    """Serve index.html for root path."""
    try:
        return send_from_directory(_FRONTEND, "index.html")
    except Exception as e:
        logger.error(f"Error serving index.html: {e}")
        return f"Error: {e}", 500


@app.route("/<path:filename>")
def serve_static(filename):
    """Serve static files (for any other requests, fallback to index.html)."""
    try:
        # Try to serve the requested file
        return send_from_directory(_FRONTEND, filename)
    except:
        # If file doesn't exist, serve index.html (SPA fallback)
        return send_from_directory(_FRONTEND, "index.html")


@app.get("/health")
def health():
    """Health check endpoint for monitoring."""
    try:
        # Check DB connection
        with _conn() as con:
            con.execute("SELECT 1").fetchone()

        return jsonify({
            "status": "ok",
            "version": "2.0.0",
            "database": "connected",
            "frontend_path": _FRONTEND,
            "index_exists": os.path.exists(os.path.join(_FRONTEND, "index.html"))
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "error",
            "version": "2.0.0",
            "database": "disconnected",
            "error": str(e)
        }), 503


# ─── POST /api/parse ──────────────────────────────────────────────────────────

@app.route("/api/parse", methods=["POST", "OPTIONS"])
def api_parse():
    """Parse uploaded document (DOCX, PDF, TXT)."""
    if request.method == "OPTIONS":
        return jsonify({}), 204

    if "file" not in request.files:
        return jsonify({"error": "No 'file' field"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    data = f.read()
    filename = f.filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    if not data:
        return jsonify({"error": "Empty file"}), 400

    logger.info(f"Parsing file: {filename} ({len(data)} bytes)")

    # 1. Convert → plain text
    try:
        plain_text = convert_to_text(filename, data)
    except ConversionError as exc:
        logger.warning(f"Conversion error for {filename}: {exc}")
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Conversion failed for {filename}: {exc}")
        return jsonify({"error": f"Conversion failed: {exc}"}), 500

    # 2. Paragraphs + chunks
    paragraphs = [l.strip() for l in plain_text.splitlines() if l.strip()]
    chunks = build_chunks(paragraphs)

    if not paragraphs:
        return jsonify({"error": "No text extracted from document"}), 422

    # 3. Store in DB
    try:
        doc_id = save_document(filename, ext, data, plain_text, paragraphs, chunks)
    except Exception as exc:
        logger.warning(f"DB save skipped: {exc}")
        doc_id = _doc_id(data)

    logger.info(f"Parsed {filename}: {len(plain_text)} chars, {len(paragraphs)} paras, {len(chunks)} chunks")

    return jsonify({
        "doc_id": doc_id,
        "filename": filename,
        "ext": ext,
        "char_count": len(plain_text),
        "para_count": len(paragraphs),
        "chunk_count": len(chunks),
        "paragraphs": paragraphs,
        "chunks": chunks,
        "plain_text": plain_text,
    })


# ─── POST /api/analyze ────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def api_analyze():
    """Analyze differences between two documents."""
    if request.method == "OPTIONS":
        return jsonify({}), 204

    body = request.get_json(silent=True) or {}
    old_parsed = body.get("old")
    new_parsed = body.get("new")
    api_key = (body.get("apiKey") or "").strip()
    model = body.get("model") or None

    # Validation
    if not isinstance(old_parsed, dict):
        return jsonify({"error": "Field 'old' required (parsed document)"}), 400
    if not isinstance(new_parsed, dict):
        return jsonify({"error": "Field 'new' required (parsed document)"}), 400
    if not api_key:
        return jsonify({"error": "Field 'apiKey' required (OpenRouter API key)"}), 400
    if not old_parsed.get("paragraphs"):
        return jsonify({"error": "'old' document has no text"}), 400
    if not new_parsed.get("paragraphs"):
        return jsonify({"error": "'new' document has no text"}), 400

    # Ensure chunks exist
    if not old_parsed.get("chunks"):
        old_parsed["chunks"] = build_chunks(old_parsed["paragraphs"])
    if not new_parsed.get("chunks"):
        new_parsed["chunks"] = build_chunks(new_parsed["paragraphs"])

    old_doc_id = old_parsed.get("doc_id", "")
    new_doc_id = new_parsed.get("doc_id", "")

    logger.info(f"Analyzing: {old_doc_id[:8]} vs {new_doc_id[:8]}")

    # Check cached analysis
    if old_doc_id and new_doc_id:
        cached = get_analysis(old_doc_id, new_doc_id)
        if cached:
            logger.info(f"Cache hit: analysis {old_doc_id[:8]}+{new_doc_id[:8]}")
            cached["_cached"] = True
            return jsonify(cached)

    # Run analysis
    try:
        result = run_analysis(old_parsed, new_parsed, api_key, model)
    except Exception as exc:
        logger.error(f"Analysis error: {exc}\n{traceback.format_exc()}")
        return jsonify({"error": f"Analysis failed: {exc}"}), 500

    # Save analysis
    if old_doc_id and new_doc_id:
        try:
            aid = save_analysis(old_doc_id, new_doc_id, result)
            result["analysis_id"] = aid
            logger.info(f"Saved analysis {aid[:8]}")
        except Exception as exc:
            logger.warning(f"Analysis DB save failed: {exc}")

    return jsonify({
        "changes": result["changes"],
        "red_zones": result["red_zones"],
        "stats": result["stats"],
        "metadata": result["metadata"],
        "synthesis": result.get("synthesis", {}),
        "analysis_id": result.get("analysis_id"),
    })


# ─── Documents CRUD ───────────────────────────────────────────────────────────

@app.get("/api/documents")
def api_list_docs():
    """List all uploaded documents."""
    try:
        docs = list_documents(100)
        return jsonify({"documents": docs, "count": len(docs)})
    except Exception as exc:
        logger.error(f"List documents failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/documents/<doc_id>")
def api_get_doc(doc_id: str):
    """Get document by ID."""
    doc = get_document(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404

    doc.pop("plain_text", None)  # Don't return full text
    return jsonify(doc)


@app.route("/api/documents/<doc_id>", methods=["DELETE", "OPTIONS"])
def api_del_doc(doc_id: str):
    """Delete document by ID."""
    if request.method == "OPTIONS":
        return jsonify({}), 204

    if not delete_document(doc_id):
        return jsonify({"error": "Document not found"}), 404

    logger.info(f"Deleted document {doc_id[:8]}")
    return jsonify({"deleted": doc_id})


# ─── Analyses ─────────────────────────────────────────────────────────────────

@app.get("/api/analyses")
def api_list_analyses():
    """List all analyses."""
    try:
        return jsonify({"analyses": list_analyses(100)})
    except Exception as exc:
        logger.error(f"List analyses failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/analyses/<analysis_id>")
def api_get_analysis(analysis_id: str):
    """Get analysis by ID."""
    with _conn() as con:
        row = con.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()

    if not row:
        return jsonify({"error": "Analysis not found"}), 404

    d = dict(row)
    for f in ("changes", "red_zones", "stats", "metadata", "synthesis"):
        d[f] = json.loads(d[f])

    return jsonify(d)


# ─── Entry ────────────────────────────────────────────────────────────────────

def create_app() -> Flask:
    """Application factory for WSGI."""
    init_db()
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    init_db()
    logger.info(f"🚀 Starting ЛексАнализ v2 on http://0.0.0.0:{port}")
    logger.info(f"📁 Database: {os.environ.get('DB_PATH', '/app/data/lexanaliz.db')}")
    app.run(host="0.0.0.0", port=port, debug=False)