"""
cache.py — кэш результатов LLM на основе SHA-256

Хранение: файл cache.json рядом со скриптом.
Ключ: sha256(old_chunk + "|||" + new_chunk)
"""

import json
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "cache.json")
_CACHE_FILE = os.path.normpath(_CACHE_FILE)

_cache: dict = {}
_loaded = False


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            logger.info("Cache loaded: %d entries", len(_cache))
        except Exception as e:
            logger.warning("Cache load failed: %s", e)
            _cache = {}
    _loaded = True


def _save() -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=None)
    except Exception as e:
        logger.warning("Cache save failed: %s", e)


def _key(old_text: str, new_text: str) -> str:
    raw = old_text + "|||" + new_text
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(old_text: str, new_text: str) -> dict | None:
    _load()
    return _cache.get(_key(old_text, new_text))


def set(old_text: str, new_text: str, result: dict) -> None:
    _load()
    _cache[_key(old_text, new_text)] = result
    _save()
