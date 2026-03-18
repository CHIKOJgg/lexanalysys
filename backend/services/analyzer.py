"""
analyzer.py — основной пайплайн анализа

Pipeline для каждой пары chunk:
    1. diff.should_skip  → пропустить если ratio > 0.95
    2. cache.get         → использовать кэш если есть
    3. openrouter.call   → вызов LLM
    4. parse JSON        → извлечь результат
    5. cache.set         → сохранить в кэш
"""

import re
import json
import logging
from typing import Any

from .diff import align_chunks
from . import cache as cache_mod
from .openrouter import call_openrouter

logger = logging.getLogger(__name__)

MAX_CHUNKS = 12   # лимит по ТЗ


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_CHUNK = (
    "You are a legal analyst for Belarusian normative acts. "
    "Return ONLY valid JSON, no markdown, no text outside JSON."
)

_USER_CHUNK_TPL = """Compare two versions of a legal document fragment.

OLD:
{old}

NEW:
{new}

Return JSON:
{{"changes":[{{"clause":"string","old_text":"string","new_text":"string","change_type":"wording|obligation|deadline|rights|addition|deletion|structural","risk_level":"green|yellow|red","law_reference":"string or null","recommendation":"string"}}],"red_zones":[{{"clause":"string","description":"string","law_reference":"string or null"}}],"summary":"string"}}

Rules:
- red: obligation change, deadline change, contradiction with higher NPA
- yellow: unclear legal consequence
- green: editorial fix
- If no changes: {{"changes":[],"red_zones":[],"summary":"no significant changes"}}"""


_SYSTEM_SYNTHESIS = (
    "You are a legal analyst. Return ONLY valid JSON, no markdown, no text outside JSON."
)

_USER_SYNTHESIS_TPL = """Summarize analysis of {n_changes} changes across document versions.

Top changes (first 20):
{changes_json}

Red zones ({n_red}):
{red_json}

Return JSON:
{{"executive_summary":"string","key_risks":["string"],"hierarchy_check":[{{"level":"string","status":"compliant|warning|violation|not_applicable","note":"string"}}]}}

hierarchy levels: Конституция РБ, Законы РБ, Декреты Президента РБ, Постановления Совмина РБ, НПА министерств"""


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict | None:
    # Strip markdown fences
    text = re.sub(r"```json\s*|```\s*", "", text).strip()
    # Find first JSON object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ── Chunk analysis ────────────────────────────────────────────────────────────

def _analyze_chunk(
    pair: dict,
    api_key: str,
    model: str | None,
    metrics: dict,
) -> dict | None:
    old_text = pair["old_text"]
    new_text = pair["new_text"]

    # 1. Check diff
    if pair["skip"]:
        return None

    # 2. Check cache
    cached = cache_mod.get(old_text, new_text)
    if cached is not None:
        metrics["cache_hits"] += 1
        logger.info("Cache hit chunk=%d", pair["index"])
        return cached

    # 3. Minimize tokens: strip excess whitespace
    old_clean = re.sub(r"\s+", " ", old_text).strip()
    new_clean = re.sub(r"\s+", " ", new_text).strip()

    user = _USER_CHUNK_TPL.format(old=old_clean, new=new_clean)

    # 4. Call LLM
    metrics["chunks_sent"] += 1
    try:
        raw = call_openrouter(api_key, _SYSTEM_CHUNK, user, model)
        result = _extract_json(raw)
        if result is None:
            logger.warning("Invalid JSON from LLM chunk=%d raw=%s",
                           pair["index"], raw[:200])
            return None
    except Exception as e:
        logger.error("OpenRouter error chunk=%d: %s", pair["index"], e)
        return None

    # Record model used (from first successful call)
    if not metrics.get("model_used"):
        metrics["model_used"] = model or "mistralai/mistral-7b-instruct"

    # 5. Save cache
    cache_mod.set(old_text, new_text, result)

    return result


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _synthesize(
    all_changes: list,
    all_red_zones: list,
    api_key: str,
    model: str | None,
) -> dict:
    user = _USER_SYNTHESIS_TPL.format(
        n_changes=len(all_changes),
        changes_json=json.dumps(all_changes[:20], ensure_ascii=False),
        n_red=len(all_red_zones),
        red_json=json.dumps(all_red_zones, ensure_ascii=False),
    )
    try:
        raw = call_openrouter(api_key, _SYSTEM_SYNTHESIS, user, model)
        result = _extract_json(raw)
        if result:
            return result
    except Exception as e:
        logger.error("Synthesis error: %s", e)
    return {
        "executive_summary": "Analysis complete. See changes table for details.",
        "key_risks": [],
        "hierarchy_check": [],
    }


# ── Main entry ────────────────────────────────────────────────────────────────

def run_analysis(
    old_parsed: dict,
    new_parsed: dict,
    api_key: str,
    model: str | None = None,
) -> dict:
    """
    Full analysis pipeline.
    Returns { changes, red_zones, stats, metadata, synthesis }.
    """
    old_chunks = old_parsed.get("chunks", [])
    new_chunks = new_parsed.get("chunks", [])

    pairs = align_chunks(old_chunks, new_chunks)

    # Limit to MAX_CHUNKS
    pairs = pairs[:MAX_CHUNKS]

    metrics: dict[str, Any] = {
        "chunks_total": len(pairs),
        "chunks_sent": 0,
        "cache_hits": 0,
        "model_used": model or FREE_MODELS_DEFAULT,
    }

    all_changes: list[dict] = []
    all_red_zones: list[dict] = []

    for pair in pairs:
        result = _analyze_chunk(pair, api_key, model, metrics)
        if result is None:
            continue
        all_changes.extend(result.get("changes") or [])
        all_red_zones.extend(result.get("red_zones") or [])

    # Deduplicate by clause+old_text
    seen: set[str] = set()
    unique_changes: list[dict] = []
    for c in all_changes:
        key = f"{c.get('clause')}|{c.get('old_text')}"
        if key not in seen:
            seen.add(key)
            unique_changes.append(c)

    # Stats
    stats = {
        "total_changes": len(unique_changes),
        "green_count": sum(1 for c in unique_changes if c.get("risk_level") == "green"),
        "yellow_count": sum(1 for c in unique_changes if c.get("risk_level") == "yellow"),
        "red_count": sum(1 for c in unique_changes if c.get("risk_level") == "red"),
    }

    # Synthesis (single LLM call)
    synthesis = _synthesize(unique_changes, all_red_zones, api_key, model)

    # Metadata
    metadata = {
        "old_file": old_parsed.get("filename"),
        "new_file": new_parsed.get("filename"),
        "old_chars": old_parsed.get("char_count"),
        "new_chars": new_parsed.get("char_count"),
        "old_chunks": len(old_chunks),
        "new_chunks": len(new_chunks),
        **metrics,
    }

    logger.info(
        "Analysis done chunks_total=%d chunks_sent=%d cache_hits=%d changes=%d",
        metrics["chunks_total"], metrics["chunks_sent"],
        metrics["cache_hits"], len(unique_changes),
    )

    return {
        "changes": unique_changes,
        "red_zones": all_red_zones,
        "stats": stats,
        "metadata": metadata,
        "synthesis": synthesis,
    }


FREE_MODELS_DEFAULT = "mistralai/mistral-7b-instruct"
