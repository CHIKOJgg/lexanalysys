# server.py — ЛексАнализ backend v2
# Pipeline: Upload → converter (docx/pdf→txt) → chunker → DB → analyze → DB

from __future__ import annotations
import json, logging, os, sys, traceback
from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.converter import ConversionError, convert_to_text
from backend.services.chunker   import build_chunks
from backend.services.analyzer  import run_analysis
from backend.db.database import (
    init_db, save_document, get_document, list_documents, delete_document,
    save_analysis, get_analysis, list_analyses, _doc_id, _conn,
)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lexanaliz")

_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=_FRONTEND, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


@app.after_request
def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def _frontend(path: str):
    fp = os.path.join(_FRONTEND, path)
    if path and os.path.exists(fp):
        return send_from_directory(_FRONTEND, path)
    return send_from_directory(_FRONTEND, "index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "version": "2.0.0"})


# ─── POST /api/parse ──────────────────────────────────────────────────────────

@app.route("/api/parse", methods=["POST", "OPTIONS"])
def api_parse():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    if "file" not in request.files:
        return jsonify({"error": "No 'file' field"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    data     = f.read()
    filename = f.filename
    ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    if not data:
        return jsonify({"error": "Empty file"}), 400

    # 1. Convert → plain text
    try:
        plain_text = convert_to_text(filename, data)
    except ConversionError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        logger.error("Conversion: %s", exc)
        return jsonify({"error": f"Conversion failed: {exc}"}), 500

    # 2. Paragraphs + chunks
    paragraphs = [l.strip() for l in plain_text.splitlines() if l.strip()]
    chunks     = build_chunks(paragraphs)
    if not paragraphs:
        return jsonify({"error": "No text extracted"}), 422

    # 3. Store in DB
    try:
        doc_id = save_document(filename, ext, data, plain_text, paragraphs, chunks)
    except Exception as exc:
        logger.warning("DB save skipped: %s", exc)
        doc_id = _doc_id(data)

    logger.info("Parsed %s: %d chars %d paras %d chunks doc=%s",
                filename, len(plain_text), len(paragraphs), len(chunks), doc_id[:10])

    return jsonify({
        "doc_id":      doc_id,
        "filename":    filename,
        "ext":         ext,
        "char_count":  len(plain_text),
        "para_count":  len(paragraphs),
        "chunk_count": len(chunks),
        "paragraphs":  paragraphs,
        "chunks":      chunks,
        "plain_text":  plain_text,
    })


# ─── POST /api/analyze ────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def api_analyze():
    if request.method == "OPTIONS":
        return jsonify({}), 204

    body = request.get_json(silent=True) or {}
    old_parsed = body.get("old")
    new_parsed  = body.get("new")
    api_key     = (body.get("apiKey") or "").strip()
    model       = body.get("model") or None

    if not isinstance(old_parsed, dict): return jsonify({"error": "Field 'old' required"}), 400
    if not isinstance(new_parsed, dict):  return jsonify({"error": "Field 'new' required"}), 400
    if not api_key:                        return jsonify({"error": "Field 'apiKey' required"}), 400
    if not old_parsed.get("paragraphs"):  return jsonify({"error": "'old' has no text"}), 400
    if not new_parsed.get("paragraphs"):  return jsonify({"error": "'new' has no text"}), 400

    if not old_parsed.get("chunks"):
        old_parsed["chunks"] = build_chunks(old_parsed["paragraphs"])
    if not new_parsed.get("chunks"):
        new_parsed["chunks"] = build_chunks(new_parsed["paragraphs"])

    old_doc_id = old_parsed.get("doc_id", "")
    new_doc_id = new_parsed.get("doc_id", "")

    # Check cached analysis
    if old_doc_id and new_doc_id:
        cached = get_analysis(old_doc_id, new_doc_id)
        if cached:
            logger.info("Cached analysis hit %s+%s", old_doc_id[:8], new_doc_id[:8])
            cached["_cached"] = True
            return jsonify(cached)

    try:
        result = run_analysis(old_parsed, new_parsed, api_key, model)
    except Exception as exc:
        logger.error("Analysis: %s\n%s", exc, traceback.format_exc())
        return jsonify({"error": f"Analysis failed: {exc}"}), 500

    if old_doc_id and new_doc_id:
        try:
            aid = save_analysis(old_doc_id, new_doc_id, result)
            result["analysis_id"] = aid
        except Exception as exc:
            logger.warning("Analysis DB save: %s", exc)

    return jsonify({
        "changes":     result["changes"],
        "red_zones":   result["red_zones"],
        "stats":       result["stats"],
        "metadata":    result["metadata"],
        "synthesis":   result.get("synthesis", {}),
        "analysis_id": result.get("analysis_id"),
    })


# ─── Documents CRUD ───────────────────────────────────────────────────────────

@app.get("/api/documents")
def api_list_docs():
    try:
        docs = list_documents(100)
        return jsonify({"documents": docs, "count": len(docs)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.get("/api/documents/<doc_id>")
def api_get_doc(doc_id: str):
    doc = get_document(doc_id)
    if not doc: return jsonify({"error": "Not found"}), 404
    doc.pop("plain_text", None)
    return jsonify(doc)

@app.route("/api/documents/<doc_id>", methods=["DELETE", "OPTIONS"])
def api_del_doc(doc_id: str):
    if request.method == "OPTIONS": return jsonify({}), 204
    if not delete_document(doc_id): return jsonify({"error": "Not found"}), 404
    return jsonify({"deleted": doc_id})


# ─── Analyses ─────────────────────────────────────────────────────────────────

@app.get("/api/analyses")
def api_list_analyses():
    try:
        return jsonify({"analyses": list_analyses(100)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.get("/api/analyses/<analysis_id>")
def api_get_analysis(analysis_id: str):
    with _conn() as con:
        row = con.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row: return jsonify({"error": "Not found"}), 404
    d = dict(row)
    for f in ("changes","red_zones","stats","metadata","synthesis"):
        d[f] = json.loads(d[f])
    return jsonify(d)


# ─── Entry ────────────────────────────────────────────────────────────────────

def create_app() -> Flask:
    init_db()
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    init_db()
    logger.info("Starting ЛексАнализ v2 on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)