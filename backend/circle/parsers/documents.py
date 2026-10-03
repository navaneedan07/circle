"""Document text extraction: .txt .html .pdf .docx (and .csv/.json as text).

Extracted text becomes a Document + searchable memory chunks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from circle.parsers.common import ParseResult


def extract_text(path: Path) -> tuple[str, str]:
    """Returns (title, text). Raises ValueError when nothing extractable."""
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        text = path.read_bytes().decode("utf-8", errors="replace")
        title = _first_line(text) or path.stem
        return title, text.strip()
    if ext in (".html", ".htm"):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(path.read_bytes().decode("utf-8", errors="replace"), "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else path.stem
        return title or path.stem, re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "") for p in reader.pages[:200]]
        text = "\n".join(pages).strip()
        if not text:
            raise ValueError("PDF has no extractable text (scanned document?)")
        return path.stem, text
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        paras = [p.text for p in d.paragraphs if p.text.strip()]
        text = "\n".join(paras).strip()
        if not text:
            raise ValueError("DOCX has no extractable text")
        return d.paragraphs[0].text.strip()[:120] if paras else path.stem, text
    if ext == ".csv":
        return path.stem, path.read_bytes().decode("utf-8", errors="replace").strip()
    if ext == ".json":
        raw = path.read_bytes().decode("utf-8", errors="replace")
        try:
            obj = json.loads(raw)
            return path.stem, json.dumps(obj, indent=2, ensure_ascii=False)[:200000]
        except json.JSONDecodeError:
            return path.stem, raw.strip()
    raise ValueError(f"unsupported document type: {ext}")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:120]
    return ""


def document_result(path: Path) -> ParseResult:
    """Uniform entry used by the pipeline for document-ish files."""
    title, text = extract_text(path)
    result = ParseResult()
    result.documents.append((path.name, title, text))
    return result
