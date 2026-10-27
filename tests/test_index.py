"""Tokenisation and BM25 ranking."""

from __future__ import annotations

import unittest

from studybuddy.index import BM25, tokenize


class TestTokenize(unittest.TestCase):
    def test_chinese_bigrams(self) -> None:
        self.assertEqual(tokenize("导数与微分"), ["导数", "数与", "与微", "微分"])

    def test_single_chinese_character(self) -> None:
        self.assertEqual(tokenize("函"), ["函"])

    def test_latin_is_lowercased(self) -> None:
        self.assertEqual(tokenize("Derivative Rules"), ["derivative", "rules"])

    def test_mixed_language(self) -> None:
        tokens = tokenize("RAG 是检索增强生成")
        self.assertIn("rag", tokens)
        self.assertIn("检索", tokens)
        self.assertIn("生成", tokens)

    def test_numbers_and_decimals(self) -> None:
        self.assertEqual(tokenize("3.14"), ["3.14"])

    def test_empty(self) -> None:
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize("   "), [])


class TestBM25(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = [
            tokenize("导数是函数在某一点的瞬时变化率"),
            tokenize("定积分是求和式的极限"),
            tokenize("矩阵乘法不满足交换律"),
        ]
        self.index = BM25(self.documents)

    def test_relevant_document_scores_highest(self) -> None:
        scores = self.index.scores("导数")
        self.assertGreater(scores[0], scores[1])
        self.assertEqual(max(range(3), key=lambda i: scores[i]), 0)

    def test_top_k(self) -> None:
        ranked = self.index.top_k("导数", k=1)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][0], 0)

    def test_allowed_filter(self) -> None:
        ranked = self.index.top_k("导数", k=5, allowed={2})
        self.assertEqual(ranked, [])

    def test_unknown_terms_do_not_match(self) -> None:
        self.assertEqual(self.index.top_k("量子力学", k=3), [])

    def test_empty_corpus(self) -> None:
        index = BM25([])
        self.assertEqual(index.scores("x"), [])
        self.assertEqual(index.top_k("x"), [])

    def test_empty_query(self) -> None:
        self.assertEqual(self.index.top_k(""), [])


if __name__ == "__main__":
    unittest.main()
