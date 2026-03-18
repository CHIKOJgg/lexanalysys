# converter.py
# Layer 1: raw bytes -> clean plain text
# This sits BEFORE chunker/LLM so models only see plain text.
#
# Pipeline:
#   DOCX -> python-docx (tables + paragraphs) -> txt
#   PDF  -> pdfplumber (layout-aware) -> fallback pypdf -> txt
#   TXT  -> utf-8 / cp1251 decode -> txt

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


# ── DOCX ─────────────────────────────────────────────────────────────────────


def _docx_to_text(data: bytes) -> str:
    """Extract paragraphs + table rows from DOCX preserving order, no duplicates."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("word/document.xml") as f:
            root = ET.parse(f).getroot()

    lines: list[str] = []

    # Track which elements have been consumed as table cells
    # so we don't double-emit them as paragraphs
    consumed: set[int] = set()

    for child in root.iter():
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        # Table row → emit as pipe-separated line
        if tag == "tr":
            cells: list[str] = []
            for tc in child.iter(f"{{{W}}}tc"):
                cell_parts = [t.text for t in tc.iter(f"{{{W}}}t") if t.text]
                cell_text = " ".join(cell_parts).strip()
                if cell_text:
                    cells.append(cell_text)
                # Mark all paragraphs inside this cell as consumed
                for p in tc.iter(f"{{{W}}}p"):
                    consumed.add(id(p))
            if cells:
                lines.append(" | ".join(cells))

        # Standalone paragraph (not inside a table cell)
        elif tag == "p":
            if id(child) in consumed:
                continue
            parts = [t.text for t in child.iter(f"{{{W}}}t") if t.text]
            line = "".join(parts).strip()
            if line:
                lines.append(line)

    return "\n".join(lines)


# ── PDF ──────────────────────────────────────────────────────────────────────


def _pdf_to_text(data: bytes) -> str:
    """Extract text from PDF: pdfplumber (layout-aware) -> pypdf fallback."""
    text = ""

    # Primary: pdfplumber (best for multi-column, tables)
    try:
        import pdfplumber

        pages: list[str] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                if page_text.strip():
                    pages.append(page_text)
        text = "\n".join(pages)
    except Exception:
        pass

    # Fallback: pypdf
    if len(text.strip()) < 100:
        try:
            import pypdf

            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(page_text)
            text = "\n".join(pages)
        except Exception:
            pass

    return text


# ── TXT ──────────────────────────────────────────────────────────────────────


def _txt_to_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# ── Normalise ─────────────────────────────────────────────────────────────────


def _normalise(raw: str) -> str:
    """Clean up text so LLM gets consistent input."""
    # Unify line endings
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove non-printable chars (keep CJK/Cyrillic/Latin/punctuation)
    text = re.sub(r"[^\S\n]+", " ", text)          # multiple spaces -> one
    text = re.sub(r" +\n", "\n", text)              # trailing spaces
    return text.strip()


# ── Public API ────────────────────────────────────────────────────────────────


class ConversionError(RuntimeError):
    pass


def convert_to_text(filename: str, data: bytes) -> str:
    """
    Convert any supported document to clean plain text.
    Returns UTF-8 string ready for chunking / LLM.
    Raises ConversionError on failure.
    """
    ext = Path(filename).suffix.lstrip(".").lower()

    try:
        if ext == "docx":
            raw = _docx_to_text(data)
        elif ext == "pdf":
            raw = _pdf_to_text(data)
        elif ext in ("txt", "text", ""):
            raw = _txt_to_text(data)
        else:
            # Try as text for unknown extensions
            raw = _txt_to_text(data)

        text = _normalise(raw)

        if len(text) < 20:
            raise ConversionError(
                f"Too little text extracted from '{filename}' ({len(text)} chars). "
                "File may be scanned image, encrypted, or empty."
            )

        return text

    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"Failed to convert '{filename}': {exc}") from exc