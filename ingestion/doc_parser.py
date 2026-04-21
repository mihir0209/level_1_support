"""
Document parser for brand-uploaded knowledge files.

Supports: PDF, DOCX, TXT, MD, HTML
Returns a list of text chunks with source metadata.
Each chunk becomes an input to the same LLM extraction pipeline used for web pages.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Max characters per chunk — matches what _extract_chunk handles
_CHUNK_SIZE = 6000
# Overlap between consecutive chunks from the same section
_CHUNK_OVERLAP = 300


def _split_text(text: str, source: str, page: int | None = None) -> list[dict[str, Any]]:
    """Split a long text into overlapping chunks preserving source metadata."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= _CHUNK_SIZE:
        return [{"text": text, "source": source, "page": page}]

    chunks = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        chunk = text[start:end]
        chunks.append({"text": chunk, "source": source, "page": page})
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


# ─── PDF ──────────────────────────────────────────────────────────────────────

def parse_pdf(path: str) -> list[dict[str, Any]]:
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError("PyMuPDF not installed — run: pip install pymupdf")

    doc = fitz.open(path)
    chunks: list[dict[str, Any]] = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().strip()
        if text:
            chunks.extend(_split_text(text, source=path, page=page_num + 1))
    doc.close()
    log.info("pdf_parsed", path=path, pages=len(doc), chunks=len(chunks))
    return chunks


# ─── DOCX ─────────────────────────────────────────────────────────────────────

def parse_docx(path: str) -> list[dict[str, Any]]:
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx not installed — run: pip install python-docx")

    doc = Document(path)
    chunks: list[dict[str, Any]] = []
    current_section: list[str] = []
    section_name = "document"

    for para in doc.paragraphs:
        style = para.style.name.lower()
        text = para.text.strip()
        if not text:
            continue

        if "heading" in style:
            # Flush current section and start a new one
            if current_section:
                joined = "\n".join(current_section)
                chunks.extend(_split_text(joined, source=path, page=None))
                current_section = []
            section_name = text
            current_section.append(text)
        else:
            current_section.append(text)

    if current_section:
        chunks.extend(_split_text("\n".join(current_section), source=path, page=None))

    log.info("docx_parsed", path=path, chunks=len(chunks))
    return chunks


# ─── TXT / MD ─────────────────────────────────────────────────────────────────

def parse_text(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    chunks = _split_text(text, source=path)
    log.info("text_parsed", path=path, chunks=len(chunks))
    return chunks


# ─── HTML ─────────────────────────────────────────────────────────────────────

def parse_html(path: str) -> list[dict[str, Any]]:
    try:
        import trafilatura
    except ImportError:
        raise ImportError("trafilatura not installed — run: pip install trafilatura")

    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    text = trafilatura.extract(raw, include_tables=True, include_links=False) or raw
    chunks = _split_text(text.strip(), source=path)
    log.info("html_parsed", path=path, chunks=len(chunks))
    return chunks


# ─── Dispatcher ───────────────────────────────────────────────────────────────

_PARSERS = {
    ".pdf":  parse_pdf,
    ".docx": parse_docx,
    ".doc":  parse_docx,
    ".txt":  parse_text,
    ".md":   parse_text,
    ".html": parse_html,
    ".htm":  parse_html,
}

SUPPORTED_EXTENSIONS = set(_PARSERS.keys())


def parse_file(path: str) -> list[dict[str, Any]]:
    """
    Parse a single document file into text chunks.
    Each chunk: {text, source, page (or None)}
    """
    ext = Path(path).suffix.lower()
    parser = _PARSERS.get(ext)
    if not parser:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    return parser(path)


def parse_files(paths: list[str]) -> list[dict[str, Any]]:
    """Parse multiple files, tagging each chunk with its source path."""
    all_chunks: list[dict[str, Any]] = []
    for path in paths:
        try:
            all_chunks.extend(parse_file(path))
        except Exception as e:
            log.error("doc_parse_failed", path=path, error=str(e))
    return all_chunks
