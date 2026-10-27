"""The library: ingest, persist, search, resolve, remove."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studybuddy import Library
from studybuddy.errors import StudyError
from support import make_textbook


class TestLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "store"
        self.pdf = make_textbook(self.tmp / "calculus.pdf")

    def test_add_book(self) -> None:
        library = Library(self.root)
        book = library.add_book(self.pdf, course="高等数学")
        self.assertEqual(book.title, "calculus")
        self.assertEqual(book.course, "高等数学")
        self.assertGreater(book.chunks, 0)
        self.assertEqual(len(library.chunks), book.chunks)
        self.assertTrue((self.root / "chunks.jsonl").exists())

    def test_progress_callback(self) -> None:
        messages: list[str] = []
        Library(self.root).add_book(self.pdf, on_progress=messages.append)
        self.assertTrue(any("reading" in message for message in messages))
        self.assertTrue(any("indexed" in message for message in messages))

    def test_search_across_books(self) -> None:
        library = Library(self.root)
        library.add_book(self.pdf, course="高等数学", title="高等数学")
        hits = library.search("derivative", top_k=3)
        self.assertTrue(hits)
        chunk, score = hits[0]
        self.assertEqual(chunk.book_title, "高等数学")
        self.assertGreater(score, 0)
        self.assertEqual(chunk.page, 1)

    def test_search_respects_book_filter(self) -> None:
        library = Library(self.root)
        library.add_book(self.pdf, title="高等数学")
        second_pdf = make_textbook(self.tmp / "linear.pdf")
        second = library.add_book(second_pdf, title="线性代数")
        hits = library.search("derivative", books=[library.get_book(second.title)], top_k=5)
        self.assertTrue(hits)
        self.assertEqual({chunk.book_id for chunk, _ in hits}, {second.id})

    def test_persistence_round_trip(self) -> None:
        library = Library(self.root)
        book = library.add_book(self.pdf, title="高等数学")

        reopened = Library(self.root)
        self.assertEqual(len(reopened.books), 1)
        self.assertEqual(reopened.books[0].id, book.id)
        self.assertEqual(len(reopened.chunks), book.chunks)
        self.assertTrue(reopened.search("derivative"))

    def test_duplicate_add_replaces(self) -> None:
        library = Library(self.root)
        first = library.add_book(self.pdf, title="高等数学")
        second = library.add_book(self.pdf, title="高等数学")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(library.books), 1)

    def test_remove_book(self) -> None:
        library = Library(self.root)
        book = library.add_book(self.pdf, title="高等数学")
        removed = library.remove_book(book.id)
        self.assertEqual(removed.id, book.id)
        self.assertEqual(library.books, [])
        self.assertEqual(library.chunks, [])

    def test_resolve_by_title_and_id(self) -> None:
        library = Library(self.root)
        book = library.add_book(self.pdf, title="高等数学")
        self.assertEqual(library.get_book("高等数学").id, book.id)
        self.assertEqual(library.get_book(book.id).id, book.id)
        self.assertEqual(library.get_book("高等").id, book.id)

    def test_resolve_ambiguous(self) -> None:
        library = Library(self.root)
        library.add_book(self.pdf, title="高等数学 上册")
        other = make_textbook(self.tmp / "other.pdf")
        library.add_book(other, title="高等数学 下册")
        with self.assertRaises(StudyError) as ctx:
            library.get_book("数学")
        self.assertIn("有歧义", str(ctx.exception))

    def test_resolve_missing(self) -> None:
        library = Library(self.root)
        library.add_book(self.pdf, title="高等数学")
        with self.assertRaises(StudyError):
            library.get_book("物理")

    def test_empty_library_is_clear(self) -> None:
        library = Library(self.root)
        with self.assertRaises(StudyError) as ctx:
            library.get_book("anything")
        self.assertIn("书库是空的", str(ctx.exception))

    def test_stats(self) -> None:
        library = Library(self.root)
        library.add_book(self.pdf, title="高等数学")
        stats = library.stats()
        self.assertEqual(stats["books"], 1)
        self.assertEqual(stats["pages"], 2)
        self.assertGreater(stats["chunks"], 0)

    def test_per_book_cap(self) -> None:
        library = Library(self.root)
        library.add_book(self.pdf, title="书甲")
        library.add_book(make_textbook(self.tmp / "b.pdf"), title="书乙")
        hits = library.search("derivative rule", top_k=4, per_book_cap=1)
        self.assertLessEqual(len({chunk.book_id for chunk, _ in hits}), 2)


if __name__ == "__main__":
    unittest.main()
