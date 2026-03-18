"""
diff.py — предварительная фильтрация чанков без вызова LLM

Алгоритм:
    ratio = SequenceMatcher(None, old_text, new_text).ratio()
    ratio > 0.95  →  skip (идентичные или почти идентичные)
    оба пустые    →  skip
    иначе         →  отправить в LLM
"""

from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.95


def should_skip(old_text: str, new_text: str) -> bool:
    """Return True if chunk pair can be skipped (no significant diff)."""
    old = old_text.strip()
    new = new_text.strip()

    # Both empty
    if not old and not new:
        return True

    # One empty — definitely changed
    if not old or not new:
        return False

    ratio = SequenceMatcher(None, old, new).ratio()
    return ratio > SIMILARITY_THRESHOLD


def align_chunks(
    old_chunks: list[dict],
    new_chunks: list[dict],
) -> list[dict]:
    """
    Pair old and new chunks by relative position.
    Returns list of { index, old_text, new_text, skip }.
    """
    count = max(len(old_chunks), len(new_chunks))
    pairs: list[dict] = []

    for i in range(count):
        old_text = old_chunks[i]["text"] if i < len(old_chunks) else ""
        new_text = new_chunks[i]["text"] if i < len(new_chunks) else ""
        pairs.append({
            "index": i,
            "old_text": old_text,
            "new_text": new_text,
            "skip": should_skip(old_text, new_text),
        })

    return pairs
