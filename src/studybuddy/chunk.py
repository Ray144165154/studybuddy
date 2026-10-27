"""Split a book into retrievable chunks that still know where they came from.

Every chunk keeps ``book_id``, ``page`` and ``section`` so an answer can always be
traced back to a real page of a real textbook. Chunks never span pages: that keeps
citations exact at the cost of a little redundancy.
"""

from __future__ import annotations

import re

from .types import Book, Chunk

__all__ = ["detect_section", "Chunker", "chunk_document"]

# Chinese textbooks: 第三章 / 第二节 / 第 3 章, then "3.1 导数的概念", "1.2.3 Theorem".
_SECTION_PATTERNS = (
    re.compile(r"^\s*(第\s*[一二三四五六七八九十百千零〇\d]+\s*[章节篇讲])\s*(.{0,40})$"),
    re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s+(\S.{0,40})$"),
    re.compile(r"^\s*((?:Chapter|Section|Part)\s+[\dIVXLC]+)\b\s*(.{0,40})$", re.IGNORECASE),
)

_LIST_MARKER = re.compile(r"^\s*(?:[-*•·]|\d+[.)]|[（(]\d+[)）])\s+")
_SENTENCE_END = re.compile(r"(?<=[。！？；])\s*|(?<=[.!?;])\s+")


def detect_section(line: str) -> str | None:
    """Return a heading if ``line`` looks like one, else ``None``.

    Heuristics, deliberately conservative: headings are short, start with a
    numbering scheme, and are not list items or sentences.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return None
    if _LIST_MARKER.match(stripped):
        return None
    if stripped.endswith(("。", "；", "，", ",", ";")):
        return None

    for pattern in _SECTION_PATTERNS:
        match = pattern.match(stripped)
        if match:
            number = match.group(1).strip()
            title = (match.group(2) or "").strip(" .．、:：")
            title = re.sub(r"\s+", " ", title)[:40]
            return f"{number} {title}".strip()

    # A bare, short, title-cased Chinese line with no punctuation is usually a heading.
    if (
        2 <= len(stripped) <= 20
        and "\u4e00" <= stripped[0] <= "\u9fff"
        and " " not in stripped
        and stripped.endswith(("章", "节", "篇"))
    ):
        return stripped
    return None


def _split_long(text: str, max_chars: int) -> list[str]:
    """Break an over-long paragraph on sentence boundaries, then hard-split."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        while len(sentence) > max_chars:
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if sentence:
            pieces.append(sentence)
    return pieces


class Chunker:
    """Accumulates lines into chunks of roughly ``max_chars`` characters.

    Headings are recognised *anywhere* in the page, not only at the top of a
    paragraph: PDF extraction usually gives one long block per page with single
    newlines, so paragraph splitting alone would miss most section titles.

    Section state carries across pages, which is what you want for a book where a
    section starts on one page and continues on the next.
    """

    def __init__(
        self,
        book: Book,
        *,
        max_chars: int = 800,
        overlap: int = 120,
    ) -> None:
        self.book = book
        self.max_chars = max_chars
        self.overlap = overlap
        self.section = ""
        self._ordinal = 0

    def _make(self, page: int, text: str) -> Chunk:
        chunk = Chunk(
            id=f"{self.book.id}:p{page}:{self._ordinal}",
            book_id=self.book.id,
            book_title=self.book.title,
            page=page,
            section=self.section,
            text=text,
            course=self.book.course,
        )
        self._ordinal += 1
        return chunk

    def feed_page(self, page_number: int, text: str) -> list[Chunk]:
        if not text.strip():
            return []

        chunks: list[Chunk] = []
        buffer: list[str] = []
        buffered = 0
        self._ordinal = 0

        def flush() -> None:
            nonlocal buffer, buffered
            body = "\n".join(buffer).strip()
            if body:
                chunks.append(self._make(page_number, body))
            buffer, buffered = [], 0

        def emit(paragraph: str) -> None:
            nonlocal buffer, buffered
            for piece in _split_long(paragraph, self.max_chars):
                piece = piece.strip()
                if not piece:
                    continue
                if buffered + len(piece) + 1 > self.max_chars and buffer:
                    tail = "\n".join(buffer)[-self.overlap :].strip() if self.overlap else ""
                    flush()
                    if tail:
                        buffer = [tail]
                        buffered = len(tail)
                buffer.append(piece)
                buffered += len(piece) + 1

        for block in re.split(r"\n\s*\n", text):
            pending: list[str] = []
            for line in block.split("\n"):
                heading = detect_section(line)
                if heading:
                    if pending:
                        emit("\n".join(pending))
                        pending = []
                    if buffer:
                        flush()
                    self.section = heading
                    continue
                pending.append(line)
            if pending:
                emit("\n".join(pending))

        if buffer:
            flush()

        return chunks


def chunk_document(
    book: Book,
    pages: list[tuple[int, str]],
    *,
    max_chars: int = 800,
    overlap: int = 120,
) -> list[Chunk]:
    """Chunk a whole document given ``(page_number, text)`` pairs."""
    chunker = Chunker(book, max_chars=max_chars, overlap=overlap)
    out: list[Chunk] = []
    for number, text in pages:
        out.extend(chunker.feed_page(number, text))
    return out
