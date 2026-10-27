"""The local library: books on disk, chunks in JSONL, search on demand.

Everything lives under ``.studybuddy/`` next to the project, so a student can zip
that folder, move it to another machine and keep their library and their memory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from .chunk import chunk_document
from .errors import LibraryError, StudyError
from .index import BM25, tokenize
from .pdf import read_pdf
from .types import Book, Chunk

__all__ = ["Library", "slugify"]

_STATE_FILE = "library.json"
_CHUNKS_FILE = "chunks.jsonl"
_FORMAT_VERSION = 1

_SLUG_KEEP = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def slugify(text: str) -> str:
    """A filesystem- and URL-safe identifier that preserves Chinese characters."""
    cleaned = _SLUG_KEEP.sub("-", text.strip()).strip("-").lower()
    return cleaned or "book"


class Library:
    """A collection of ingested textbooks with lexical search across all of them."""

    def __init__(self, root: str | Path = ".studybuddy") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.books: list[Book] = []
        self.chunks: list[Chunk] = []
        self._bm25: BM25 | None = None
        self.load()

    # -- persistence ---------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.root / _STATE_FILE

    @property
    def chunks_path(self) -> Path:
        return self.root / _CHUNKS_FILE

    def load(self) -> None:
        self.books = []
        self.chunks = []
        self._bm25 = None

        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text("utf-8"))
            except json.JSONDecodeError as exc:
                raise LibraryError(f"corrupted {self.state_path}: {exc}") from exc
            self.books = [Book.from_dict(item) for item in data.get("books") or []]

        if self.chunks_path.exists():
            with self.chunks_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self.chunks.append(Chunk.from_dict(json.loads(line)))

    def save(self) -> None:
        payload = {"version": _FORMAT_VERSION, "books": [book.to_dict() for book in self.books]}
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        with self.chunks_path.open("w", encoding="utf-8") as handle:
            for chunk in self.chunks:
                handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
        self._bm25 = None

    # -- ingest --------------------------------------------------------------------

    def add_book(
        self,
        path: str | Path,
        *,
        course: str = "",
        title: str | None = None,
        chunk_size: int = 800,
        on_progress: Callable[[str], None] | None = None,
    ) -> Book:
        """Parse a PDF, chunk it, persist it and return the new book record."""
        file = Path(path)
        if on_progress:
            on_progress(f"reading {file.name}")
        document = read_pdf(file)

        book_title = (title or document.title or file.stem).strip()
        digest = hashlib.sha1(f"{file.name}:{file.stat().st_size}:{book_title}".encode()).hexdigest()[:6]
        book_id = f"{slugify(book_title)}-{digest}"

        book = Book(
            id=book_id,
            title=book_title,
            course=course,
            pages=document.page_count,
            source=str(file),
            added_at=datetime.now().isoformat(timespec="seconds"),
        )

        if on_progress:
            on_progress(f"chunking {document.non_empty_pages} page(s)")
        pages = [(page.number, page.text) for page in document.pages]
        new_chunks = chunk_document(book, pages, max_chars=chunk_size)
        book.chunks = len(new_chunks)

        # Re-adding the same book replaces it instead of duplicating.
        self.books = [existing for existing in self.books if existing.id != book_id]
        self.chunks = [chunk for chunk in self.chunks if chunk.book_id != book_id]
        self.books.append(book)
        self.chunks.extend(new_chunks)
        self.save()

        if on_progress:
            on_progress(f"indexed {len(new_chunks)} chunk(s)")
        return book

    def remove_book(self, reference: str) -> Book:
        book = self.get_book(reference)
        self.books = [existing for existing in self.books if existing.id != book.id]
        self.chunks = [chunk for chunk in self.chunks if chunk.book_id != book.id]
        self.save()
        return book

    # -- lookup --------------------------------------------------------------------

    def get_book(self, reference: str) -> Book:
        """Find a book by id, exact title, or a unambiguous substring of the title."""
        if not self.books:
            raise StudyError("书库是空的 -- 先用 `studybuddy add` 导入一本教材")

        for book in self.books:
            if book.id == reference:
                return book
        for book in self.books:
            if book.title == reference:
                return book
        matches = [book for book in self.books if reference.lower() in book.title.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            titles = "、".join(book.title for book in self.books)
            raise StudyError(f"没有书名匹配 {reference!r}，现有教材: {titles}")
        titles = "、".join(book.title for book in matches)
        raise StudyError(f"{reference!r} 有歧义，匹配到多本: {titles}")

    def resolve_books(self, references: Iterable[str] | None) -> list[Book]:
        if not references:
            return list(self.books)
        return [self.get_book(reference) for reference in references]

    def chunks_of(self, book_id: str) -> list[Chunk]:
        return [chunk for chunk in self.chunks if chunk.book_id == book_id]

    # -- search --------------------------------------------------------------------

    @property
    def index(self) -> BM25:
        if self._bm25 is None:
            self._bm25 = BM25([tokenize(f"{chunk.section} {chunk.text}") for chunk in self.chunks])
        return self._bm25

    def search(
        self,
        query: str,
        *,
        books: Iterable[Book] | None = None,
        top_k: int = 6,
        per_book_cap: int = 3,
        min_score: float = 0.0,
    ) -> list[tuple[Chunk, float]]:
        """Rank chunks against ``query``, optionally restricted to some books.

        ``per_book_cap`` keeps a single verbose book from monopolising the context,
        which is what makes cross-textbook answers possible.
        """
        if not self.chunks:
            return []

        allowed_ids = {book.id for book in books} if books is not None else None
        if allowed_ids is not None:
            allowed = {index for index, chunk in enumerate(self.chunks) if chunk.book_id in allowed_ids}
        else:
            allowed = None

        # Ask for more than we need, then thin out per-book duplicates.
        ranked = self.index.top_k(
            query, k=top_k * max(per_book_cap, 1) * 2, allowed=allowed, min_score=min_score
        )
        if not ranked:
            return []

        per_book: Counter[str] = Counter()
        results: list[tuple[Chunk, float]] = []
        for index, score in ranked:
            chunk = self.chunks[index]
            if per_book[chunk.book_id] >= per_book_cap:
                continue
            per_book[chunk.book_id] += 1
            results.append((chunk, score))
            if len(results) >= top_k:
                break
        return results

    # -- reporting -----------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        return {
            "books": len(self.books),
            "pages": sum(book.pages for book in self.books),
            "chunks": len(self.chunks),
            "root": str(self.root),
        }
