"""The local library: books on disk, chunks in JSONL, search on demand.

Everything lives under ``.studybuddy/`` next to the project, so a student can zip
that folder, move it to another machine and keep their library and their memory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .chunk import chunk_document
from .embed import DEFAULT_RRF_K, EmbeddingIndex, reciprocal_rank_fusion
from .errors import LibraryError, StudyError
from .index import BM25, tokenize
from .pdf import clean_text, read_pdf
from .types import Book, Chunk

__all__ = ["Library", "slugify"]

_STATE_FILE = "library.json"
_CHUNKS_FILE = "chunks.jsonl"
_EMBEDDINGS_FILE = "embeddings.jsonl"
_FORMAT_VERSION = 1

_SLUG_KEEP = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def _looks_like_heading(text: str) -> bool:
    """Reject OCR false positives such as "1 推论的条件，于是由引理1知…"."""
    stripped = text.strip()
    if not 2 <= len(stripped) <= 30:
        return False
    return not any(mark in stripped for mark in "，。；：、：,;")


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
        self.embeddings: EmbeddingIndex | None = None
        self._bm25: BM25 | None = None
        self._chunk_by_id: dict[str, Chunk] | None = None
        self.load()

    # -- persistence ---------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.root / _STATE_FILE

    @property
    def chunks_path(self) -> Path:
        return self.root / _CHUNKS_FILE

    @property
    def embeddings_path(self) -> Path:
        return self.root / _EMBEDDINGS_FILE

    def _invalidate(self) -> None:
        self._bm25 = None
        self._chunk_by_id = None

    def load(self) -> None:
        self.books = []
        self.chunks = []
        self.embeddings = None
        self._invalidate()

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

        self.embeddings = EmbeddingIndex.load(self.embeddings_path)

    def save(self) -> None:
        payload = {"version": _FORMAT_VERSION, "books": [book.to_dict() for book in self.books]}
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        with self.chunks_path.open("w", encoding="utf-8") as handle:
            for chunk in self.chunks:
                handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
        self._invalidate()

    # -- ingest --------------------------------------------------------------------

    def add_document(
        self,
        *,
        title: str,
        pages: Iterable[tuple[int, str]],
        course: str = "",
        source: str = "",
        chunk_size: int = 800,
        book_id: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> Book:
        """Index already-extracted text.

        This is the entry point for anything that is not a text-layer PDF: an OCR
        pipeline, an EPUB conversion, or notes pasted from a lecture.
        """
        page_list = [(int(number), clean_text(text)) for number, text in pages]
        if not any(text.strip() for _number, text in page_list):
            raise LibraryError("没有可索引的文字内容（这份文档可能是扫描件，需要先做 OCR）")

        book_title = (title or source or "untitled").strip()
        if book_id is None:
            digest = hashlib.sha1(f"{source}:{len(page_list)}:{book_title}".encode()).hexdigest()[:6]
            book_id = f"{slugify(book_title)}-{digest}"

        book = Book(
            id=book_id,
            title=book_title,
            course=course,
            pages=len(page_list),
            source=source,
            added_at=datetime.now().isoformat(timespec="seconds"),
        )

        if on_progress:
            on_progress(f"chunking {len(page_list)} page(s)")
        new_chunks = chunk_document(book, page_list, max_chars=chunk_size)
        book.chunks = len(new_chunks)

        # Re-adding the same book replaces it instead of duplicating.
        self.books = [existing for existing in self.books if existing.id != book_id]
        self.chunks = [chunk for chunk in self.chunks if chunk.book_id != book_id]
        self.books.append(book)
        self.chunks.extend(new_chunks)
        self.save()
        self._prune_embeddings()

        if on_progress:
            on_progress(f"indexed {len(new_chunks)} chunk(s)")
        return book

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

        return self.add_document(
            title=book_title,
            pages=[(page.number, page.text) for page in document.pages],
            course=course,
            source=str(file),
            chunk_size=chunk_size,
            book_id=f"{slugify(book_title)}-{digest}",
            on_progress=on_progress,
        )

    def remove_book(self, reference: str) -> Book:
        book = self.get_book(reference)
        self.books = [existing for existing in self.books if existing.id != book.id]
        self.chunks = [chunk for chunk in self.chunks if chunk.book_id != book.id]
        self.save()
        self._prune_embeddings()
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

    def section_leads(
        self,
        query: str,
        *,
        books: Iterable[Book] | None = None,
        limit: int = 3,
    ) -> list[Chunk]:
        """The first chunk of each section whose *title* matches the query.

        Section titles are the book's own vocabulary: "独立性" may be the right
        chapter even when no body paragraph matches the student's wording. Pulling
        the opening chunk of the best-matching sections fixes the common failure
        where the answer is in §1.6 but retrieval only surfaces its exercises.
        """
        allowed_ids = {book.id for book in books} if books is not None else None

        leads: dict[tuple[str, str], Chunk] = {}
        for chunk in self.chunks:
            if chunk.nav or not _looks_like_heading(chunk.section):
                continue
            if allowed_ids is not None and chunk.book_id not in allowed_ids:
                continue
            key = (chunk.book_id, chunk.section)
            current = leads.get(key)
            if current is None or (chunk.page, chunk.id) < (current.page, current.id):
                leads[key] = chunk

        if not leads:
            return []

        keys = list(leads)
        index = BM25([tokenize(key[1]) for key in keys])
        ranked = index.top_k(query, k=limit)
        return [leads[keys[position]] for position, _score in ranked]

    def chunks_of(self, book_id: str) -> list[Chunk]:
        return [chunk for chunk in self.chunks if chunk.book_id == book_id]

    # -- search --------------------------------------------------------------------

    @property
    def index(self) -> BM25:
        if self._bm25 is None:
            self._bm25 = BM25([tokenize(f"{chunk.section} {chunk.text}") for chunk in self.chunks])
        return self._bm25

    @property
    def chunk_map(self) -> dict[str, Chunk]:
        if self._chunk_by_id is None:
            self._chunk_by_id = {chunk.id: chunk for chunk in self.chunks}
        return self._chunk_by_id

    @property
    def has_embeddings(self) -> bool:
        return self.embeddings is not None and len(self.embeddings) > 0

    @property
    def embeddings_model(self) -> str | None:
        return self.embeddings.model if self.embeddings is not None else None

    def build_embeddings(
        self,
        embedder: Any,
        *,
        force: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Embed every chunk that does not have a vector yet. Returns how many were new."""
        if not self.chunks:
            return 0

        reuse = self.embeddings if (self.embeddings is not None and not force) else None
        if reuse is not None and reuse.model != embedder.model:
            reuse = None  # a different model means the old vectors are not comparable

        chunk_ids = [chunk.id for chunk in self.chunks]
        vectors: list[list[float] | None] = [None] * len(self.chunks)
        if reuse is not None:
            for position, chunk_id in enumerate(chunk_ids):
                cached = reuse.vector_for(chunk_id)
                if cached is not None:
                    vectors[position] = cached

        missing = [position for position, vector in enumerate(vectors) if vector is None]
        if missing:
            texts = [f"{self.chunks[position].section} {self.chunks[position].text}" for position in missing]
            embedded = embedder.embed(texts, on_progress=on_progress)
            if len(embedded) != len(missing):
                raise LibraryError("embedding provider returned the wrong number of vectors")
            for position, vector in zip(missing, embedded, strict=False):
                vectors[position] = vector

        index = EmbeddingIndex(
            embedder.model,
            chunk_ids,
            [vector or [] for vector in vectors],
        )
        self.embeddings = index
        index.save(self.embeddings_path)
        return len(missing)

    def _prune_embeddings(self) -> None:
        """Drop vectors whose chunks no longer exist (a book was removed or replaced)."""
        if self.embeddings is None:
            return
        valid = {chunk.id for chunk in self.chunks}
        kept = [chunk_id for chunk_id in self.embeddings.chunk_ids if chunk_id in valid]
        if len(kept) == len(self.embeddings.chunk_ids):
            return
        if not kept:
            self.embeddings = None
            self.embeddings_path.unlink(missing_ok=True)
            return
        vectors = [self.embeddings.vector_for(chunk_id) or [] for chunk_id in kept]
        self.embeddings = EmbeddingIndex(
            self.embeddings.model, kept, vectors, created_at=self.embeddings.created_at
        )
        self.embeddings.save(self.embeddings_path)

    def search(
        self,
        query: str,
        *,
        books: Iterable[Book] | None = None,
        top_k: int = 6,
        per_book_cap: int = 3,
        min_score: float = 0.0,
        query_vector: Sequence[float] | None = None,
        rrf_k: int = DEFAULT_RRF_K,
        include_nav: bool = False,
    ) -> list[tuple[Chunk, float]]:
        """Rank chunks against ``query``, optionally restricted to some books.

        When ``query_vector`` is supplied *and* embeddings exist, lexical and semantic
        rankings are fused with Reciprocal Rank Fusion. Otherwise this is plain BM25.

        ``per_book_cap`` keeps a single verbose book from monopolising the context,
        which is what makes cross-textbook answers possible.
        """
        if not self.chunks:
            return []

        allowed_ids = {book.id for book in books} if books is not None else None
        # The per-book cap exists to stop one verbose book from crowding out the
        # others. With only one book in scope it would only starve the context window.
        scope = len(allowed_ids) if allowed_ids is not None else len(self.books)
        if scope == 1:
            per_book_cap = max(per_book_cap, top_k)
        if allowed_ids is not None or not include_nav:
            allowed = {
                index
                for index, chunk in enumerate(self.chunks)
                if (allowed_ids is None or chunk.book_id in allowed_ids) and (include_nav or not chunk.nav)
            }
            # The vector index is keyed by chunk id, not by book id.
            allowed_chunk_ids = {self.chunks[index].id for index in allowed}
        else:
            allowed = None
            allowed_chunk_ids = None

        # Ask for more than we need, then thin out per-book duplicates.
        candidate_k = min(max(top_k * max(per_book_cap, 1) * 2, top_k, 20), 200)
        lexical = self.index.top_k(query, k=candidate_k, allowed=allowed, min_score=min_score)
        lexical_ids = [self.chunks[position].id for position, _score in lexical]

        fused: list[tuple[str, float]] = []
        if query_vector is not None and self.has_embeddings and self.embeddings is not None:
            semantic = self.embeddings.search(query_vector, k=candidate_k, allowed=allowed_chunk_ids)
            semantic_ids = [chunk_id for chunk_id, _score in semantic]
            if semantic_ids:
                fused = reciprocal_rank_fusion([lexical_ids, semantic_ids], k=rrf_k)

        if not fused:
            fused = [(self.chunks[position].id, score) for position, score in lexical]

        chunk_map = self.chunk_map
        per_book: Counter[str] = Counter()
        results: list[tuple[Chunk, float]] = []
        for chunk_id, score in fused:
            chunk = chunk_map.get(chunk_id)
            if chunk is None:  # stale vector from a removed book
                continue
            if allowed_ids is not None and chunk.book_id not in allowed_ids:
                continue
            if chunk.nav and not include_nav:  # a table of contents is not an answer
                continue
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
            "nav_chunks": sum(1 for chunk in self.chunks if chunk.nav),
            "embeddings": len(self.embeddings) if self.embeddings is not None else 0,
            "embedding_model": self.embeddings_model or "",
            "root": str(self.root),
        }
