"""
chunker.py — структурированное разбиение на чанки

Приоритет:
1. По структурным маркерам (Статья, Глава, 1.1, а))
2. Fallback — по абзацам с соблюдением лимита символов
"""

import re

CHUNK_MIN = 100   # не создавать чанки меньше этого
CHUNK_MAX = 2000  # жёсткий лимит символов на чанк

# Паттерны структурных заголовков (по убыванию приоритета)
_HEADING_RE = re.compile(
    r"^("
    r"(Статья|СТАТЬЯ|Глава|ГЛАВА|Раздел|РАЗДЕЛ|Часть|ЧАСТЬ)\s+\d+"  # Статья 1
    r"|(\d+\.)+\d*\s"          # 1.  /  1.1  /  1.1.2
    r"|\d+\.\s"                # 1.
    r"|[а-яА-Я]\)\s"          # а) б) в)
    r")",
    re.UNICODE,
)


def _is_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line.strip()))


def _flush(buf: list[str], idx: int, result: list[dict]) -> None:
    text = " ".join(buf).strip()
    # strip duplicate spaces
    text = re.sub(r" {2,}", " ", text)
    if len(text) >= CHUNK_MIN:
        result.append({
            "index": idx,
            "text": text,
            "para_count": len(buf),
        })


def build_chunks(paragraphs: list[str]) -> list[dict]:
    """
    Split paragraphs into chunks respecting structural boundaries.
    Returns list of { index, text, para_count }.

    Strategy:
    - Flush current buffer when a NEW structural heading appears AND buffer
      has already accumulated enough content (>= CHUNK_MIN chars).
    - This prevents tiny single-heading chunks on dense structured docs.
    """
    if not paragraphs:
        return []

    chunks: list[dict] = []
    buf: list[str] = []
    buf_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        is_heading = _is_heading(para)
        would_exceed = buf_len + len(para) + 1 > CHUNK_MAX

        # Flush conditions:
        # 1. Hard size limit exceeded
        # 2. New heading AND buffer already has enough content
        if buf and (would_exceed or (is_heading and buf_len >= CHUNK_MIN)):
            _flush(buf, len(chunks), chunks)
            buf = []
            buf_len = 0

        buf.append(para)
        buf_len += len(para) + 1

    # Last chunk
    if buf:
        _flush(buf, len(chunks), chunks)

    return chunks
