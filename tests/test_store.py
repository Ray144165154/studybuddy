"""The library: ingest, persist, search, resolve, remove."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studybuddy import Library
from studybuddy.errors import LibraryError, StudyError
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


class TestAddDocument(unittest.TestCase):
    """Ingesting text extracted elsewhere: OCR output, EPUB, pasted notes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = Library(self.tmp / "store")

    def test_indexes_supplied_pages(self) -> None:
        book = self.library.add_document(
            title="概率论基础",
            pages=[
                (1, "第一章 事件与概率\n概率是事件发生可能性的度量。"),
                (2, "第二章 随机变量\n随机变量是样本空间上的可测函数。"),
            ],
            course="概率论",
        )
        self.assertEqual(book.title, "概率论基础")
        self.assertEqual(book.pages, 2)
        self.assertGreater(book.chunks, 0)
        self.assertEqual({chunk.page for chunk in self.library.chunks_of(book.id)}, {1, 2})

    def test_sections_are_detected(self) -> None:
        book = self.library.add_document(
            title="书",
            pages=[(1, "第一章 事件与概率\n概率的定义。"), (2, "1.1 古典概型\n等可能样本点。")],
        )
        sections = {chunk.section for chunk in self.library.chunks_of(book.id)}
        self.assertIn("第一章 事件与概率", sections)
        self.assertIn("1.1 古典概型", sections)

    def test_is_searchable(self) -> None:
        self.library.add_document(title="书", pages=[(1, "随机变量的数学期望与方差")])
        hits = self.library.search("方差")
        self.assertTrue(hits)

    def test_empty_text_is_rejected(self) -> None:
        with self.assertRaises(LibraryError) as ctx:
            self.library.add_document(title="空", pages=[(1, "   "), (2, "")])
        self.assertIn("OCR", str(ctx.exception))

    def test_same_id_replaces(self) -> None:
        first = self.library.add_document(title="书", pages=[(1, "旧内容")])
        second = self.library.add_document(title="书", pages=[(1, "新内容")])
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.library.books), 1)
        self.assertIn("新内容", self.library.chunks[0].text)

    def test_explicit_book_id(self) -> None:
        book = self.library.add_document(title="书", pages=[(1, "内容")], book_id="custom-id")
        self.assertEqual(book.id, "custom-id")

    def test_progress_callback(self) -> None:
        messages: list[str] = []
        self.library.add_document(title="书", pages=[(1, "内容")], on_progress=messages.append)
        self.assertTrue(any("indexed" in message for message in messages))

    def test_navigation_pages_are_skipped_by_default(self) -> None:
        self.library.add_document(
            title="书",
            pages=[
                (1, "目录\n" + "\n".join(f"3.{i} 条件概率讨论 {i * 10}" for i in range(1, 30))),
                (2, "第三章 条件概率\n条件概率的定义是 P(A|B)=P(AB)/P(B)。"),
            ],
        )
        hits = self.library.search("条件概率", top_k=5)
        self.assertTrue(hits)
        self.assertTrue(all(not chunk.nav for chunk, _score in hits))
        self.assertEqual(hits[0][0].page, 2)

    def test_navigation_can_be_included(self) -> None:
        self.library.add_document(
            title="书",
            pages=[(1, "目录\n" + "\n".join(f"3.{i} 条目 {i * 10}" for i in range(1, 30)))],
        )
        self.assertEqual(self.library.search("条目", top_k=3), [])
        self.assertTrue(self.library.search("条目", top_k=3, include_nav=True))


if __name__ == "__main__":
    unittest.main()
