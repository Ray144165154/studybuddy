"""Tokenisation and BM25 ranking."""

from __future__ import annotations

import time
import unittest

from studybuddy.index import BM25, tokenize


class TestTokenize(unittest.TestCase):
    def test_chinese_bigrams(self) -> None:
        self.assertEqual(
            tokenize("导数与微分"),
            ["导数", "数与", "与微", "微分", "导", "数", "与", "微", "分"],
        )

    def test_chinese_characters_are_kept(self) -> None:
        # Single characters are what makes "熵" / "什么是熵" retrievable at all;
        # bigrams alone can never match a term at the edge of a question.
        self.assertEqual(tokenize("熵增"), ["熵增", "熵", "增"])

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

    def test_index_build_and_search_stay_fast(self) -> None:
        # The unigram fallback must not make normal retrieval slow: it is only
        # attempted when the bigram index finds nothing at all.
        documents = [tokenize(f"第{i}章 导数的定义与计算，导数是瞬时变化率。") for i in range(300)]
        started = time.perf_counter()
        index = BM25(documents)
        for query in ["导数的定义", "什么是熵", "矩阵", "chain rule"]:
            index.top_k(query, k=10)
        self.assertLess(time.perf_counter() - started, 2.0)


class TestSingleCharacterRecall(unittest.TestCase):
    """One-character Chinese terms used to be unfindable: the query bigram around
    the term ("是熵") can never match the book bigram ("熵是")."""

    def setUp(self) -> None:
        self.documents = [
            tokenize("3.2 熵的定义 熵是系统混乱程度的度量，熵增原理说明孤立系统的熵不会减少。"),
            tokenize("2.1 矩阵的秩 矩阵的秩等于线性无关列向量的最大个数。"),
        ]
        self.index = BM25(self.documents)

    def test_single_character_term_matches(self) -> None:
        self.assertEqual([position for position, _score in self.index.top_k("熵", k=2)], [0])

    def test_what_is_question_with_one_character_term_matches(self) -> None:
        self.assertEqual([position for position, _score in self.index.top_k("什么是熵", k=2)], [0])
        self.assertEqual([position for position, _score in self.index.top_k("什么是秩", k=2)], [1])

    def test_fallback_requires_every_content_character(self) -> None:
        # "量子纠缠" must not match merely because one page contains "向量".
        self.assertEqual(self.index.top_k("量子纠缠", k=2), [])

    def test_function_characters_alone_do_not_match(self) -> None:
        self.assertEqual(self.index.top_k("为什么", k=2), [])

    def test_bigram_ranking_is_unchanged(self) -> None:
        # The fallback must not leak into normal queries: "熵增" still ranks by
        # its bigram match, not by the single characters.
        self.assertEqual([position for position, _score in self.index.top_k("熵增", k=2)], [0])


if __name__ == "__main__":
    unittest.main()
