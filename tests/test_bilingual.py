"""Chinese and English textbooks must both survive the whole pipeline.

The Chinese fixture was produced by a real PDF producer (CUPS) and, like many
real-world Chinese PDFs, it maps glyphs to Kangxi Radicals (⾃) instead of the
unified characters (自). That is invisible on screen but breaks retrieval unless
the text is normalised -- so this file is the regression test for it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studybuddy import Library, StudyBuddy, read_pdf, tokenize
from support import ScriptedLLM, make_textbook, says

MEMORY_JSON = '{"topics": ["导数"], "facts": [], "summary": "问了一个问题"}'

FIXTURE = Path(__file__).parent / "fixtures" / "zh_sample.pdf"

KANGXI = range(0x2E80, 0x2FE0)
CJK_COMPAT = range(0xF900, 0xFB00)


class TestChinesePdf(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = Library(self.tmp / "store")

    def test_fixture_exists(self) -> None:
        self.assertTrue(FIXTURE.exists(), f"missing fixture: {FIXTURE}")

    def test_extracts_chinese_text(self) -> None:
        document = read_pdf(FIXTURE)
        text = document.full_text()
        self.assertIn("导数", text)
        self.assertIn("链式法则", text)
        self.assertEqual(document.page_count, 1)

    def test_kangxi_radicals_are_normalised(self) -> None:
        text = read_pdf(FIXTURE).full_text()
        radicals = [char for char in text if ord(char) in KANGXI or ord(char) in CJK_COMPAT]
        self.assertEqual(radicals, [], f"unnormalised compatibility characters: {radicals}")
        self.assertIn("自变量", text)
        self.assertIn("某一点", text)

    def test_chinese_sections_are_detected(self) -> None:
        book = self.library.add_book(FIXTURE, course="高等数学", title="高等数学")
        sections = {chunk.section for chunk in self.library.chunks_of(book.id)}
        self.assertIn("3.1 导数的概念", sections)
        self.assertIn("3.2 求导法则", sections)

    def test_chinese_search(self) -> None:
        book = self.library.add_book(FIXTURE, title="高等数学")
        hits = self.library.search("自变量的增量", top_k=2)
        self.assertTrue(hits)
        chunk, score = hits[0]
        self.assertEqual(chunk.book_id, book.id)
        self.assertEqual(chunk.page, 1)
        self.assertIn("导数", chunk.text)
        self.assertGreater(score, 0)

    def test_radical_spelling_in_the_query_still_matches(self) -> None:
        self.library.add_book(FIXTURE, title="高等数学")
        hits = self.library.search("⾃变量", top_k=2)  # deliberately the Kangxi form
        self.assertTrue(hits)
        self.assertIn("自变量", hits[0][0].text)

    def test_chinese_chunks_are_searchable_by_section(self) -> None:
        self.library.add_book(FIXTURE, title="高等数学")
        hits = self.library.search("求导法则", top_k=2)
        self.assertTrue(hits)
        self.assertIn("3.2", hits[0][0].section)


class TestBilingualTogether(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = Library(self.tmp / "store")
        self.zh = self.library.add_book(FIXTURE, course="高等数学", title="高等数学")
        self.en = self.library.add_book(
            make_textbook(self.tmp / "calculus.pdf"), course="Calculus", title="Calculus"
        )

    def test_both_books_are_indexed(self) -> None:
        self.assertEqual(len(self.library.books), 2)
        self.assertGreater(self.library.stats()["chunks"], 0)

    def test_english_query_finds_english_book(self) -> None:
        hits = self.library.search("chain rule", top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].book_id, self.en.id)

    def test_chinese_query_finds_chinese_book(self) -> None:
        hits = self.library.search("链式法则", top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].book_id, self.zh.id)

    def test_mixed_tokenizer(self) -> None:
        tokens = tokenize("导数 derivative 链式 rule")
        self.assertIn("导数", tokens)
        self.assertIn("derivative", tokens)
        self.assertIn("链式", tokens)
        self.assertIn("rule", tokens)

    def test_book_filter_is_language_independent(self) -> None:
        hits = self.library.search("rule", books=[self.library.get_book("高等数学")], top_k=3)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()


class TestEndToEndBilingual(unittest.TestCase):
    """PDF -> chunk -> retrieve -> answer with citations, in both languages."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "data"

    def _buddy(self, *responses) -> StudyBuddy:
        return StudyBuddy(self.root, model="test-model", llm=ScriptedLLM(*responses))

    def test_chinese_book_end_to_end(self) -> None:
        buddy = self._buddy(says("导数是瞬时变化率 [1]。"), says(MEMORY_JSON))
        buddy.add_book(FIXTURE, course="高等数学", title="高等数学")

        answer = buddy.ask("什么是导数？")
        rendered = answer.with_citations()

        self.assertIn("[1]", answer.text)
        self.assertIn("来源", rendered)
        self.assertIn("《高等数学》", rendered)
        self.assertIn("第1页", rendered)
        self.assertIn("导数", rendered)

    def test_english_book_end_to_end(self) -> None:
        buddy = self._buddy(says("The derivative is a rate of change [1]."), says(MEMORY_JSON))
        buddy.add_book(make_textbook(self.tmp / "calculus.pdf"), course="Calculus", title="Calculus")

        answer = buddy.ask("What is a derivative?")

        self.assertIn("[1]", answer.text)
        self.assertIn("《Calculus》", answer.with_citations())
        self.assertTrue(answer.sources)

    def test_chinese_question_against_english_book_fails_honestly(self) -> None:
        buddy = self._buddy(says("教材中未找到相关内容。"), says(MEMORY_JSON))
        buddy.add_book(make_textbook(self.tmp / "calculus.pdf"), title="Calculus")

        answer = buddy.ask("什么是导数？")
        # No Chinese content exists, so no passages are fabricated for the model.
        self.assertEqual(answer.sources, [])
        self.assertIn("未找到", answer.text)


if __name__ == "__main__":
    unittest.main()
