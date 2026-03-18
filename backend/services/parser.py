"""
parser.py — извлечение текста из DOCX / PDF / TXT
"""

import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


# ── DOCX ──────────────────────────────────────────────────────────────────────

def _parse_docx(data: bytes) -> list[str]:
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("word/document.xml") as f:
            root = ET.parse(f).getroot()
    for para in root.iter(f"{{{W}}}p"):
        parts = [t.text for t in para.iter(f"{{{W}}}t") if t.text]
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return paragraphs


# ── PDF ───────────────────────────────────────────────────────────────────────

def _parse_pdf(data: bytes) -> list[str]:
    paragraphs: list[str] = []

    # Primary: pypdf
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        lines: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())
        paragraphs = [l.strip() for l in lines if l.strip()]
    except Exception:
        paragraphs = []

    # Fallback: pdfplumber if pypdf returned too little
    if len(" ".join(paragraphs)) < 200:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    for line in text.splitlines():
                        line = line.strip()
                        if line:
                            paragraphs.append(line)
        except Exception:
            pass

    return paragraphs


# ── TXT ───────────────────────────────────────────────────────────────────────

def _parse_txt(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    return [l.strip() for l in text.splitlines() if l.strip()]


# ── Public API ────────────────────────────────────────────────────────────────

def parse_file(filename: str, data: bytes) -> dict:
    """
    Returns:
        {
            filename, ext, char_count, para_count,
            paragraphs: [str],
            chunks: []   ← populated by chunker
        }
    """
    ext = Path(filename).suffix.lstrip(".").lower()

    if ext == "docx":
        paragraphs = _parse_docx(data)
    elif ext == "pdf":
        paragraphs = _parse_pdf(data)
    else:
        paragraphs = _parse_txt(data)

    full_text = "\n".join(paragraphs)
    return {
        "filename": filename,
        "ext": ext,
        "char_count": len(full_text),
        "para_count": len(paragraphs),
        "paragraphs": paragraphs,
        "chunks": [],   # filled by chunker
    }
