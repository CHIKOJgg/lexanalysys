# analyzer.py -- pipeline: diff -> cache -> LLM -> JSON extract -> cache -> synthesis

import re
import json
import logging
import time
from typing import Any

from .diff import align_chunks
from . import cache as cache_mod
from .openrouter import call_openrouter, FREE_MODELS

logger = logging.getLogger(__name__)

MAX_CHUNKS = 12
INTER_REQUEST_DELAY = 4.5   # seconds between LLM calls (rate-limit safety margin)
FREE_MODELS_DEFAULT  = FREE_MODELS[0]   # "openrouter/free"

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_CHUNK = (
    "You are a legal analyst for Belarusian normative legal acts. "
    "Respond ONLY with valid JSON. No markdown. No text outside JSON."
)

_USER_CHUNK_TPL = (
    "Compare OLD and NEW versions of a legal document fragment. "
    "Find all changes with legal significance.\n\n"
    "OLD:\n{old}\n\nNEW:\n{new}\n\n"
    "Return this JSON (and nothing else):\n"
    '{{"changes":[{{"clause":"str","old_text":"str","new_text":"str",'
    '"change_type":"wording|obligation|deadline|rights|addition|deletion|structural",'
    '"risk_level":"green|yellow|red","law_reference":"str or null",'
    '"recommendation":"str"}}],'
    '"red_zones":[{{"clause":"str","description":"str","law_reference":"str or null"}}],'
    '"summary":"str"}}\n\n'
    "risk_level rules — red: obligation/deadline/rights change or contradiction with higher NPA; "
    "yellow: unclear legal consequence; green: editorial fix.\n"
    'No changes found? Return: {{"changes":[],"red_zones":[],"summary":"no significant changes"}}'
)

_SYSTEM_SYNTH = (
    "You are a senior legal analyst. "
    "Respond ONLY with valid JSON. No markdown."
)

_USER_SYNTH_TPL = (
    "Summarize this document comparison: {n} changes, {r} red zones.\n\n"
    "Top changes:\n{changes}\n\nRed zones:\n{red}\n\n"
    "Return this JSON (and nothing else):\n"
    '{{"executive_summary":"str","key_risks":["str"],'
    '"hierarchy_check":[{{"level":"str",'
    '"status":"compliant|warning|violation|not_applicable",'
    '"note":"str"}}]}}\n'
    "Hierarchy levels (Belarusian law, check each):\n"
    "Конституция РБ, Законы РБ, Декреты Президента РБ, "
    "Постановления Совмина РБ, НПА министерств"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict | None:
    text = re.sub(r"```json\s*|```\s*", "", text).strip()
    m = _JSON_RE.search(text)
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

    # 1. Diff filter
    if pair["skip"]:
        return None

    # 2. Cache hit
    cached = cache_mod.get(old_text, new_text)
    if cached is not None:
        metrics["cache_hits"] += 1
        logger.info("cache hit chunk=%d", pair["index"])
        return cached

    # 3. Trim tokens
    old_clean = re.sub(r"\s+", " ", old_text).strip()[:1800]
    new_clean = re.sub(r"\s+", " ", new_text).strip()[:1800]
    user = _USER_CHUNK_TPL.format(old=old_clean, new=new_clean)

    # 4. Call LLM
    metrics["chunks_sent"] += 1
    try:
        raw, used_model = call_openrouter(api_key, _SYSTEM_CHUNK, user, model)
        # Track the actual model used (openrouter/free resolves to a real model)
        metrics["model_used"] = used_model
        result = _extract_json(raw)
        if result is None:
            logger.warning("invalid JSON chunk=%d raw=%r", pair["index"], raw[:300])
            metrics["chunks_sent"] -= 1
            return None
    except Exception as exc:
        logger.error("LLM error chunk=%d: %s", pair["index"], exc)
        metrics["chunks_sent"] -= 1
        return None

    # 5. Cache save
    cache_mod.set(old_text, new_text, result)
    return result


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _synthesize(
    changes: list,
    red_zones: list,
    api_key: str,
    model: str | None,
) -> dict:
    user = _USER_SYNTH_TPL.format(
        n=len(changes),
        r=len(red_zones),
        changes=json.dumps(changes[:15], ensure_ascii=False),
        red=json.dumps(red_zones[:10], ensure_ascii=False),
    )
    try:
        raw, _ = call_openrouter(api_key, _SYSTEM_SYNTH, user, model)
        result = _extract_json(raw)
        if result:
            return result
    except Exception as exc:
        logger.error("synthesis error: %s", exc)
    return {
        "executive_summary": "Analysis complete. Review the changes table for details.",
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
    old_chunks = old_parsed.get("chunks", [])
    new_chunks = new_parsed.get("chunks", [])
    pairs = align_chunks(old_chunks, new_chunks)[:MAX_CHUNKS]

    metrics: dict[str, Any] = {
        "chunks_total": len(pairs),
        "chunks_sent":  0,
        "cache_hits":   0,
        "model_used":   model or FREE_MODELS_DEFAULT,
    }

    all_changes:   list[dict] = []
    all_red_zones: list[dict] = []
    llm_calls = 0

    for pair in pairs:
        result = _analyze_chunk(pair, api_key, model, metrics)
        if result is None:
            continue
        # Delay AFTER a real LLM call (not after cache/skip)
        llm_calls += 1
        if llm_calls > 1:
            time.sleep(INTER_REQUEST_DELAY)
        all_changes.extend(result.get("changes") or [])
        all_red_zones.extend(result.get("red_zones") or [])

    # Deduplicate changes
    seen: set[str] = set()
    unique_changes: list[dict] = []
    for c in all_changes:
        key = f"{c.get('clause')}|{str(c.get('old_text', ''))[:60]}"
        if key not in seen:
            seen.add(key)
            unique_changes.append(c)

    stats = {
        "total_changes": len(unique_changes),
        "green_count":   sum(1 for c in unique_changes if c.get("risk_level") == "green"),
        "yellow_count":  sum(1 for c in unique_changes if c.get("risk_level") == "yellow"),
        "red_count":     sum(1 for c in unique_changes if c.get("risk_level") == "red"),
    }

    # Synthesis (1 extra LLM call)
    if llm_calls > 0:
        time.sleep(INTER_REQUEST_DELAY)
    synthesis = _synthesize(unique_changes, all_red_zones, api_key, model)

    metadata = {
        "old_file":   old_parsed.get("filename"),
        "new_file":   new_parsed.get("filename"),
        "old_chars":  old_parsed.get("char_count"),
        "new_chars":  new_parsed.get("char_count"),
        "old_chunks": len(old_chunks),
        "new_chunks": len(new_chunks),
        **metrics,
    }

    logger.info(
        "done total=%d sent=%d cache=%d changes=%d model=%s",
        metrics["chunks_total"], metrics["chunks_sent"],
        metrics["cache_hits"], len(unique_changes),
        metrics["model_used"],
    )

    return {
        "changes":   unique_changes,
        "red_zones": all_red_zones,
        "stats":     stats,
        "metadata":  metadata,
        "synthesis": synthesis,
    }