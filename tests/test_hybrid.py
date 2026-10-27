"""Hybrid retrieval: does semantic search actually add anything, and does it degrade?

The point of these tests is not that RRF is implemented -- it is that a query with
*zero lexical overlap* can still find the right page, while a query with lexical
overlap keeps working even when the embedding service is down.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studybuddy import Book, Chunk, Library, Memory, StudyBuddy
from studybuddy.errors import LLMError, StudyError
from support import ScriptedLLM, says

MEMORY_JSON = '{"topics": [], "facts": [], "summary": "s"}'


class FakeEmbedder:
    """Deterministic embeddings, with optional failure injection."""

    model = "fake-embed"

    def __init__(
        self, mapping: dict[str, list[float]] | None = None, *, dim: int = 3, fail: bool = False
    ) -> None:
        self.mapping = {key.strip(): value for key, value in (mapping or {}).items()}
        self.dim = dim
        self.fail = fail
        self.query_calls = 0
        self.embed_calls = 0

    def embed(self, texts, on_progress=None):  # type: ignore[no-untyped-def]
        self.embed_calls += 1
        vectors = [self._vector(text) for text in texts]
        if on_progress is not None:
            on_progress(len(vectors), len(vectors))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        self.query_calls += 1
        if self.fail:
            raise LLMError("embedding service down")
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        key = text.strip()
        if key in self.mapping:
            return list(self.mapping[key])
        vector = [0.0] * self.dim
        for char in key:
            vector[ord(char) % self.dim] += 1.0
        return vector


def make_library(root: Path) -> Library:
    library = Library(root)
    library.books = [Book(id="math-1", title="高等数学", course="高等数学", pages=2, chunks=2)]
    library.chunks = [
        Chunk(
            id="math-1:p1:0",
            book_id="math-1",
            book_title="高等数学",
            page=1,
            section="",
            course="高等数学",
            text="矩阵乘法不满足交换律",
        ),
        Chunk(
            id="math-1:p2:0",
            book_id="math-1",
            book_title="高等数学",
            page=2,
            section="",
            course="高等数学",
            text="导数是瞬时变化率",
        ),
    ]
    library.save()
    return library


VECTORS = {
    "矩阵乘法不满足交换律": [1.0, 0.0, 0.0],
    "导数是瞬时变化率": [0.0, 1.0, 0.0],
    # The paraphrase a student would actually type.
    "为什么 AB 不等于 BA": [0.95, 0.05, 0.0],
}


class TestSemanticRetrieval(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = make_library(self.tmp / "store")
        self.embedder = FakeEmbedder(VECTORS)

    def test_lexical_search_misses_the_paraphrase(self) -> None:
        hits = self.library.search("为什么 AB 不等于 BA")
        self.assertEqual(hits, [])

    def test_semantic_search_finds_the_paraphrase(self) -> None:
        self.library.build_embeddings(self.embedder)
        query_vector = [0.95, 0.05, 0.0]
        hits = self.library.search("为什么 AB 不等于 BA", query_vector=query_vector)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].id, "math-1:p1:0")

    def test_fusion_without_vectors_is_plain_lexical(self) -> None:
        self.library.build_embeddings(self.embedder)
        hits = self.library.search("导数")
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].id, "math-1:p2:0")

    def test_both_rankings_contribute(self) -> None:
        self.library.build_embeddings(self.embedder)
        # Lexically about derivatives, semantically about matrices.
        hits = self.library.search("导数", query_vector=[0.9, 0.1, 0.0], top_k=2)
        ids = [chunk.id for chunk, _score in hits]
        self.assertIn("math-1:p2:0", ids)  # from BM25
        self.assertIn("math-1:p1:0", ids)  # from the vector side

    def test_book_filter_applies_to_semantic_side(self) -> None:
        self.library.build_embeddings(self.embedder)
        other = self.library.books[0]
        hits = self.library.search(
            "为什么 AB 不等于 BA",
            books=[other],
            query_vector=[0.95, 0.05, 0.0],
        )
        self.assertTrue(hits, "semantic results must survive a book filter")
        self.assertTrue(all(chunk.book_id == "math-1" for chunk, _score in hits))


class TestEmbeddingLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = make_library(self.tmp / "store")

    def test_build_and_persist(self) -> None:
        added = self.library.build_embeddings(FakeEmbedder(VECTORS))
        self.assertEqual(added, 2)
        self.assertTrue(self.library.embeddings_path.exists())
        self.assertTrue(self.library.has_embeddings)
        self.assertEqual(self.library.embeddings_model, "fake-embed")

        reopened = Library(self.tmp / "store")
        self.assertEqual(len(reopened.embeddings or []), 2)
        self.assertEqual(reopened.embeddings_model, "fake-embed")

    def test_second_build_reuses_vectors(self) -> None:
        embedder = FakeEmbedder(VECTORS)
        self.library.build_embeddings(embedder)
        self.assertEqual(self.library.build_embeddings(embedder), 0)
        self.assertEqual(embedder.embed_calls, 1)

    def test_force_rebuilds(self) -> None:
        embedder = FakeEmbedder(VECTORS)
        self.library.build_embeddings(embedder)
        self.assertEqual(self.library.build_embeddings(embedder, force=True), 2)
        self.assertEqual(embedder.embed_calls, 2)

    def test_model_change_rebuilds(self) -> None:
        self.library.build_embeddings(FakeEmbedder(VECTORS))

        class OtherEmbedder(FakeEmbedder):
            model = "other-embed"

        added = self.library.build_embeddings(OtherEmbedder(VECTORS))
        self.assertEqual(added, 2)
        self.assertEqual(self.library.embeddings_model, "other-embed")

    def test_removing_a_book_prunes_vectors(self) -> None:
        self.library.build_embeddings(FakeEmbedder(VECTORS))
        self.library.remove_book("math-1")
        self.assertFalse(self.library.has_embeddings)
        self.assertFalse(self.library.embeddings_path.exists())

    def test_empty_library_is_a_noop(self) -> None:
        empty = Library(self.tmp / "empty")
        self.assertEqual(empty.build_embeddings(FakeEmbedder(VECTORS)), 0)


class TestStudyBuddySemantic(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "data"
        self.library = make_library(self.root)
        self.memory = Memory(self.root)

    def buddy(self, embedder, *responses) -> StudyBuddy:
        return StudyBuddy(
            self.root,
            model="test-model",
            llm=ScriptedLLM(*responses),
            library=self.library,
            memory=self.memory,
            embedder=embedder,
        )

    def test_ask_uses_semantic_candidates(self) -> None:
        embedder = FakeEmbedder(VECTORS)
        buddy = self.buddy(embedder, says("答 [1]"), says(MEMORY_JSON))
        buddy.build_index()

        answer = buddy.ask("为什么 AB 不等于 BA")
        self.assertTrue(answer.sources)
        self.assertEqual(answer.sources[0].chunk.id, "math-1:p1:0")
        self.assertEqual(embedder.query_calls, 1)

    def test_semantic_can_be_disabled(self) -> None:
        embedder = FakeEmbedder(VECTORS)
        buddy = self.buddy(embedder, says("答"), says(MEMORY_JSON))
        buddy.build_index()

        buddy.ask("为什么 AB 不等于 BA", semantic=False)
        self.assertEqual(embedder.query_calls, 0)

    def test_embedding_outage_falls_back_to_lexical(self) -> None:
        good = FakeEmbedder(VECTORS)
        buddy = self.buddy(good, says("答 [1]"), says(MEMORY_JSON))
        buddy.build_index()

        buddy.embedder = FakeEmbedder(VECTORS, fail=True)
        answer = buddy.ask("导数")
        self.assertTrue(answer.sources)  # lexical still worked
        self.assertTrue(answer.warnings)
        self.assertIn("语义检索不可用", answer.warnings[0])

    def test_build_index_without_config_explains_itself(self) -> None:
        buddy = StudyBuddy(self.root, llm=ScriptedLLM(says("x")), library=self.library, memory=self.memory)
        with self.assertRaises(StudyError) as ctx:
            buddy.build_index()
        self.assertIn("embedding", str(ctx.exception))

    def test_compare_uses_semantic_side(self) -> None:
        embedder = FakeEmbedder(VECTORS)
        buddy = self.buddy(embedder, says("对比 [1]"), says(MEMORY_JSON))
        buddy.build_index()

        result = buddy.compare("AB 与 BA", "导数")
        self.assertTrue(result.sources)
        self.assertEqual(embedder.query_calls, 2)


if __name__ == "__main__":
    unittest.main()
