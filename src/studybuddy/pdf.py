"""Turn a PDF textbook into clean, page-addressable text.

Only text-based PDFs are supported. Scanned books need OCR first -- the error says
so explicitly instead of silently returning an empty library.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PDFError

__all__ = ["PdfPage", "PdfDocument", "read_pdf", "clean_text", "repair_surrogates"]

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
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def non_empty_pages(self) -> int:
        return sum(1 for page in self.pages if not page.is_empty)

    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if not page.is_empty)


def repair_surrogates(text: str) -> str:
    """Join UTF-16 surrogate halves that pypdf leaks for maths symbols.

    PDFs with italic maths glyphs (𝑥 U+1D465, 𝑛 U+1D45B) sometimes map them through
    a ToUnicode CMap that pypdf decodes into two *lone* surrogates instead of one
    astral character. A `str` holding lone surrogates looks fine in memory but
    cannot be encoded as UTF-8, so writing the chunk index raised
    ``UnicodeEncodeError: surrogates not allowed`` and the whole import failed.

    Encoding with ``surrogatepass`` and decoding again recombines valid pairs and
    turns the genuinely broken ones into U+FFFD, which is both visible and safe.
    The cheap ``encode`` probe keeps the fast path free of extra copies.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text


def clean_text(text: str) -> str:
    """Repair the usual extraction artefacts (PDF or OCR) without touching the words."""
    text = repair_surrogates(text)
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
            unlocked = reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise PDFError(f"PDF is encrypted and could not be opened: {file.name}") from exc
        if not unlocked:
            raise PDFError(f"PDF 需要密码：{file.name}。请先解密并另存为无密码 PDF，再导入。")

    pages: list[PdfPage] = []
    failed_pages: list[int] = []
    try:
        for number, page in enumerate(reader.pages, start=1):
            try:
                raw = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one broken page must not kill the book
                failed_pages.append(number)
                raw = ""
            pages.append(PdfPage(number=number, text=clean_text(raw)))
    except Exception as exc:  # noqa: BLE001 - malformed page trees fail during iteration
        raise PDFError(f"PDF 页结构损坏，无法读取：{file.name}。请用 PDF 阅读器重新导出后再导入。") from exc

    if not any(not page.is_empty for page in pages):
        if failed_pages:
            raise PDFError(
                f"PDF 文字解析失败：{file.name}（{len(failed_pages)} 页提取异常）。"
                "请用 PDF 阅读器重新导出，或运行 OCR 后再导入。"
            )
        raise PDFError(
            f"no selectable text found in {file.name}. "
            "It looks like a scanned book -- run OCR (e.g. ocrmypdf) and try again."
        )

    metadata: Any = reader.metadata or {}
    title = repair_surrogates(str(metadata.get("/Title") or "")).strip()
    author = repair_surrogates(str(metadata.get("/Author") or "")).strip()

    warnings = []
    if failed_pages:
        numbers = "、".join(map(str, failed_pages[:20]))
        warnings.append(
            f"部分页面解析失败（共 {len(failed_pages)} 页）：{numbers}。"
            "这些页面未参与检索，请重新导出或 OCR 后导入。"
        )
    return PdfDocument(path=file, pages=pages, title=title, author=author, warnings=warnings)
