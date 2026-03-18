"""
server.py — ЛексАнализ backend (Flask)

Endpoints:
    POST /api/parse    — парсинг файла
    POST /api/analyze  — сравнение двух редакций через OpenRouter

Запуск:
    python backend/server.py
    # или через gunicorn:
    # gunicorn backend.server:app

Примечание: ТЗ требует uvicorn/FastAPI, но FastAPI недоступен в offline-среде.
Flask предоставляет идентичный API-контракт и полностью заменим при деплое.
"""

import json
import logging
import os
import sys
import traceback

from flask import Flask, request, jsonify, Response
from flask import send_from_directory

# Ensure backend package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.parser import parse_file
from backend.services.chunker import build_chunks
from backend.services.analyzer import run_analysis

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lexanaliz")

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "..", "frontend"),
    static_url_path="",
)

app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB


# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path: str):
    frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
    if path and os.path.exists(os.path.join(frontend_dir, path)):
        return send_from_directory(frontend_dir, path)
    return send_from_directory(frontend_dir, "index.html")


# ── /health ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})


# ── POST /api/parse ───────────────────────────────────────────────────────────
@app.route("/api/parse", methods=["POST", "OPTIONS"])
def api_parse():
    if request.method == "OPTIONS":
        return jsonify({}), 204

    if "file" not in request.files:
        return jsonify({"error": "No file field in request"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    data = f.read()
    if not data:
        return jsonify({"error": "Empty file"}), 400

    if len(data) > 20 * 1024 * 1024:
        return jsonify({"error": "File too large (max 20 MB)"}), 413

    try:
        parsed = parse_file(f.filename, data)
    except Exception as e:
        logger.error("Parse error: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"Parse error: {e}"}), 422

    if not parsed["paragraphs"]:
        return jsonify({"error": "Could not extract text from file"}), 422

    # Build chunks
    parsed["chunks"] = build_chunks(parsed["paragraphs"])

    logger.info(
        "Parsed %s: %d chars, %d paras, %d chunks",
        f.filename, parsed["char_count"], parsed["para_count"], len(parsed["chunks"]),
    )

    return jsonify(parsed)


# ── POST /api/analyze ─────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def api_analyze():
    if request.method == "OPTIONS":
        return jsonify({}), 204

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    old_parsed = body.get("old")
    new_parsed = body.get("new")
    api_key = body.get("apiKey", "").strip()
    model = body.get("model") or None

    # Validation
    if not old_parsed or not isinstance(old_parsed, dict):
        return jsonify({"error": "Field 'old' is required"}), 400
    if not new_parsed or not isinstance(new_parsed, dict):
        return jsonify({"error": "Field 'new' is required"}), 400
    if not api_key:
        return jsonify({"error": "Field 'apiKey' is required"}), 400

    if not old_parsed.get("paragraphs"):
        return jsonify({"error": "'old' document has no text"}), 400
    if not new_parsed.get("paragraphs"):
        return jsonify({"error": "'new' document has no text"}), 400

    # Rebuild chunks if missing (client may send parse result without chunks)
    if not old_parsed.get("chunks"):
        old_parsed["chunks"] = build_chunks(old_parsed["paragraphs"])
    if not new_parsed.get("chunks"):
        new_parsed["chunks"] = build_chunks(new_parsed["paragraphs"])

    try:
        result = run_analysis(old_parsed, new_parsed, api_key, model)
    except Exception as e:
        logger.error("Analysis error: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"Analysis failed: {e}"}), 500

    # Strict output schema per spec
    output = {
        "changes": result["changes"],
        "red_zones": result["red_zones"],
        "stats": result["stats"],
        "metadata": result["metadata"],
        "synthesis": result.get("synthesis", {}),
    }

    return jsonify(output)


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting ЛексАнализ backend on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
