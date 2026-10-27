"""Turn a PDF textbook into clean, page-addressable text.

Only text-based PDFs are supported. Scanned books need OCR first -- the error says
so explicitly instead of silently returning an empty library.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PDFError

__all__ = ["PdfPage", "PdfDocument", "read_pdf", "clean_text"]

# A line that is only a page number, e.g. "42", "- 42 -", "第 42 页".
_PAGE_NUMBER_LINE = re.compile(r"^[\s\-—–·•∙⋅・]*(\d{1,4}|第\s*\d{1,4}\s*页)[\s\-—–·•∙⋅・]*$")
# Words split across a line break: "deriv-\native" -> "derivative".
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_MULTI_SPACE = re.compile(r"[ \t\u00a0\u3000]+")

# Some PDF producers map CJK glyphs to Kangxi Radicals (⾃ U+2F83) or CJK
# Compatibility Ideographs instead of the unified characters (自 U+81EA). That is
# invisible to a human but fatal to retrieval: the bigram "⾃变" never matches "自变".
# NFKC maps exactly those blocks back, without touching full-width punctuation.
_COMPAT_RANGES = ((0x2E80, 0x2FDF), (0xF900, 0xFAFF), (0x2F800, 0x2FA1F))
_COMPAT_TABLE = {
    codepoint: unicodedata.normalize("NFKC", chr(codepoint))
    for start, end in _COMPAT_RANGES
    for codepoint in range(start, end + 1)
    if unicodedata.normalize("NFKC", chr(codepoint)) != chr(codepoint)
}
_MULTI_NEWLINE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class PdfPage:
    """One page of extracted text."""

    number: int  # 1-based
    text: str

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(slots=True)
class PdfDocument:
    """The whole extraction result, plus the metadata textbooks usually carry."""

    path: Path
    pages: list[PdfPage]
    title: str = ""
    author: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def non_empty_pages(self) -> int:
        return sum(1 for page in self.pages if not page.is_empty)

    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if not page.is_empty)


def clean_text(text: str) -> str:
    """Repair the usual extraction artefacts (PDF or OCR) without touching the words."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_COMPAT_TABLE)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00ad", "")  # soft hyphen
    text = _HYPHEN_BREAK.sub(r"\1\2", text)

    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _PAGE_NUMBER_LINE.match(stripped):
            continue
        lines.append(_MULTI_SPACE.sub(" ", line).rstrip())

    text = "\n".join(lines)
    # Paragraphs separated by a blank line; never more than one.
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def read_pdf(path: str | Path) -> PdfDocument:
    """Extract text page by page from ``path``.

    Raises
    ------
    PDFError
        The file is missing, not a PDF, encrypted, or contains no selectable text.
    """
    file = Path(path)
    if not file.exists():
        raise PDFError(f"file not found: {file}")
    if file.stat().st_size == 0:
        raise PDFError(f"file is empty: {file}")

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PDFError("pypdf is required: pip install pypdf") from exc

    try:
        reader = PdfReader(str(file))
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types
        raise PDFError(f"cannot open PDF {file.name}: {exc}") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise PDFError(f"PDF is encrypted and could not be opened: {file.name}") from exc

    pages: list[PdfPage] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one broken page must not kill the book
            raw = ""
        pages.append(PdfPage(number=number, text=clean_text(raw)))

    if not any(not page.is_empty for page in pages):
        raise PDFError(
            f"no selectable text found in {file.name}. "
            "It looks like a scanned book -- run OCR (e.g. ocrmypdf) and try again."
        )

    metadata: Any = reader.metadata or {}
    title = str(metadata.get("/Title") or "").strip()
    author = str(metadata.get("/Author") or "").strip()

    return PdfDocument(path=file, pages=pages, title=title, author=author)
