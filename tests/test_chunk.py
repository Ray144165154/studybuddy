"""Chunking: heading detection, page attribution, size limits."""

from __future__ import annotations

import unittest

from studybuddy.chunk import Chunker, chunk_document, detect_section, is_navigation_text
from studybuddy.types import Book


def book() -> Book:
    return Book(id="b1", title="高等数学", course="高等数学")


class TestDetectSection(unittest.TestCase):
    def test_chinese_chapter(self) -> None:
        self.assertEqual(detect_section("第三章 导数与微分"), "第三章 导数与微分")

    def test_numbered_chinese_section(self) -> None:
        self.assertEqual(detect_section("3.1 导数的概念"), "3.1 导数的概念")

    def test_english_chapter(self) -> None:
        self.assertEqual(detect_section("Chapter 3 Derivatives"), "Chapter 3 Derivatives")

    def test_deep_numbering(self) -> None:
        self.assertTrue(detect_section("1.2.3 定理与证明"))

    def test_list_items_are_not_headings(self) -> None:
        self.assertIsNone(detect_section("1. 求导"))
        self.assertIsNone(detect_section("- 导数的定义"))

    def test_sentences_are_not_headings(self) -> None:
        self.assertIsNone(detect_section("导数是函数在某一点的瞬时变化率。"))
        self.assertIsNone(detect_section("The derivative measures the rate of change."))

    def test_long_line_is_not_heading(self) -> None:
        self.assertIsNone(detect_section("3.1 " + "很长的标题" * 20))

    def test_blank(self) -> None:
        self.assertIsNone(detect_section("   "))


class TestNavigationPages(unittest.TestCase):
    def test_table_of_contents(self) -> None:
        text = "目录\n第1章 概率与测度 1\n3.1 导数的概念 12"
        self.assertTrue(is_navigation_text(text))

    def test_index_page(self) -> None:
        text = "内容索引\n" + "\n".join(f"条目{i} 1.2.{i}" for i in range(40))
        self.assertTrue(is_navigation_text(text))

    def test_body_page_is_not_navigation(self) -> None:
        text = "定理3 设随机变量序列独立同分布，则其部分和服从大数定律。" * 6
        self.assertFalse(is_navigation_text(text))

    def test_numbered_exercises_are_not_navigation(self) -> None:
        # 40 short numbered items, but they do not end in page references.
        text = "\n".join(f"{i}. 证明随机变量的期望具有线性性质" for i in range(1, 41))
        self.assertFalse(is_navigation_text(text))

    def test_empty(self) -> None:
        self.assertFalse(is_navigation_text("   "))

    def test_chunker_marks_nav_chunks(self) -> None:
        toc = "目录\n" + "\n".join(f"{i}.{j} 小节标题 {i * 10 + j}" for i in range(1, 6) for j in range(1, 8))
        chunks = Chunker(book()).feed_page(1, toc)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.nav for chunk in chunks))

    def test_chunker_marks_body_chunks(self) -> None:
        chunks = Chunker(book()).feed_page(1, "定理3 设随机变量序列独立同分布。" * 5)
        self.assertFalse(any(chunk.nav for chunk in chunks))

    def test_section_trailing_decoration_is_stripped(self) -> None:
        self.assertEqual(detect_section("第1章 概率与测度・"), "第1章 概率与测度")


class TestChunker(unittest.TestCase):
    def test_chunk_keeps_page_and_id(self) -> None:
        chunks = chunk_document(book(), [(1, "第一段内容。\n\n第二段内容。")])
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].page, 1)
        self.assertEqual(chunks[0].book_title, "高等数学")
        self.assertTrue(chunks[0].id.startswith("b1:p1:"))

    def test_heading_mid_page_updates_section(self) -> None:
        text = "导言内容。\n3.1 导数的概念\n导数是瞬时变化率。"
        chunks = Chunker(book()).feed_page(1, text)
        self.assertTrue(any(chunk.section == "3.1 导数的概念" for chunk in chunks))
        joined = "".join(chunk.text for chunk in chunks)
        self.assertNotIn("3.1 导数的概念", joined)

    def test_section_carries_across_pages(self) -> None:
        chunker = Chunker(book())
        first = chunker.feed_page(1, "3.1 导数的概念\n这一页开始讲导数。")
        second = chunker.feed_page(2, "这一页继续讲导数的性质。")
        self.assertEqual(first[0].section, "3.1 导数的概念")
        self.assertEqual(second[0].section, "3.1 导数的概念")

    def test_decimal_numbering_is_not_split(self) -> None:
        chunks = Chunker(book()).feed_page(1, "3.1 导数的概念\n定义如下。")
        for chunk in chunks:
            self.assertNotIn("3.\n", chunk.text)

    def test_long_page_is_split_into_several_chunks(self) -> None:
        paragraph = "这是一句测试用的中文内容。" * 80
        chunks = Chunker(book(), max_chars=300, overlap=40).feed_page(1, paragraph)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 400)

    def test_empty_page(self) -> None:
        self.assertEqual(Chunker(book()).feed_page(3, "   \n\n "), [])

    def test_overlap_carries_context(self) -> None:
        first = "甲" * 250
        second = "乙" * 250
        chunks = Chunker(book(), max_chars=300, overlap=50).feed_page(1, f"{first}\n\n{second}")
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(any("甲" in chunk.text for chunk in chunks[1:]))

    def test_ordinals_are_unique_per_page(self) -> None:
        chunks = Chunker(book(), max_chars=50).feed_page(1, "内容" * 200)
        ids = [chunk.id for chunk in chunks]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
