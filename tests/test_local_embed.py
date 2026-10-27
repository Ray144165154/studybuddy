"""The optional local embedding backend (fastembed).

The heavy dependency is stubbed out, so this file runs everywhere -- including CI,
where the extra is deliberately not installed.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from studybuddy import Library, Memory, StudyBuddy
from studybuddy.errors import StudyError
from studybuddy.local_embed import DEFAULT_LOCAL_MODEL, LocalEmbedder, is_available


class FakeTextEmbedding:
    """Mimics the small slice of ``fastembed.TextEmbedding`` that we call."""

    instances: list[FakeTextEmbedding] = []

    def __init__(self, model_name: str, **kwargs: object) -> None:
        self.model_name = model_name
        self.batch_sizes: list[int] = []
        FakeTextEmbedding.instances.append(self)

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.batch_sizes.append(len(texts))
        return [[float(len(text)), float(index)] for index, text in enumerate(texts)]


def stub_fastembed():
    module = types.SimpleNamespace(TextEmbedding=FakeTextEmbedding)
    return patch.dict(sys.modules, {"fastembed": module})


class TestAvailability(unittest.TestCase):
    def test_missing_extra_is_reported_honestly(self) -> None:
        with patch.dict(sys.modules, {"fastembed": None}):
            self.assertFalse(is_available())

    def test_present_extra_is_detected(self) -> None:
        with stub_fastembed():
            self.assertTrue(is_available())

    def test_error_message_mentions_the_extra(self) -> None:
        with patch.dict(sys.modules, {"fastembed": None}):
            embedder = LocalEmbedder()
            with self.assertRaises(StudyError) as ctx:
                embedder.embed(["x"])
        self.assertIn("local-embed", str(ctx.exception))


class TestLocalEmbedder(unittest.TestCase):
    def test_provider_marker(self) -> None:
        self.assertEqual(LocalEmbedder().provider, "local")

    def test_default_model(self) -> None:
        self.assertEqual(LocalEmbedder().model, DEFAULT_LOCAL_MODEL)

    def test_embeds_in_order(self) -> None:
        with stub_fastembed():
            vectors = LocalEmbedder().embed(["ab", "abcd"])
        self.assertEqual(vectors, [[2.0, 0.0], [4.0, 1.0]])

    def test_batching(self) -> None:
        FakeTextEmbedding.instances.clear()
        with stub_fastembed():
            embedder = LocalEmbedder(batch_size=2)
            vectors = embedder.embed(["a", "b", "c", "d", "e"])
        self.assertEqual(len(vectors), 5)
        self.assertEqual(FakeTextEmbedding.instances[0].batch_sizes, [2, 2, 1])

    def test_progress_callback(self) -> None:
        seen: list[tuple[int, int]] = []
        with stub_fastembed():
            LocalEmbedder(batch_size=2).embed(["a", "b", "c"], on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(2, 3), (3, 3)])

    def test_blank_text_is_kept(self) -> None:
        with stub_fastembed():
            vectors = LocalEmbedder().embed(["", "  ", "x"])
        self.assertEqual(len(vectors), 3)

    def test_empty_input(self) -> None:
        with stub_fastembed():
            self.assertEqual(LocalEmbedder().embed([]), [])

    def test_model_is_lazy(self) -> None:
        FakeTextEmbedding.instances.clear()
        embedder = LocalEmbedder()
        self.assertEqual(FakeTextEmbedding.instances, [])
        with stub_fastembed():
            embedder.embed(["x"])
        self.assertEqual(len(FakeTextEmbedding.instances), 1)

    def test_engine_is_reused(self) -> None:
        FakeTextEmbedding.instances.clear()
        with stub_fastembed():
            embedder = LocalEmbedder()
            embedder.embed(["x"])
            embedder.embed(["y"])
        self.assertEqual(len(FakeTextEmbedding.instances), 1)

    def test_model_load_failure_is_wrapped(self) -> None:
        class Broken:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise ValueError("unknown model")

        with (
            patch.dict(sys.modules, {"fastembed": types.SimpleNamespace(TextEmbedding=Broken)}),
            self.assertRaises(StudyError) as ctx,
        ):
            LocalEmbedder("nope").embed(["x"])
        self.assertIn("无法加载", str(ctx.exception))


class TestLibraryWithLocalProvider(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = Library(self.tmp / "store")
        self.library.add_document(
            title="书",
            pages=[(1, "条件概率的定义与性质"), (2, "随机变量的数学期望")],
        )

    def test_provider_is_persisted(self) -> None:
        with stub_fastembed():
            self.library.build_embeddings(LocalEmbedder())
        self.assertEqual(self.library.embeddings_provider, "local")

        reopened = Library(self.tmp / "store")
        self.assertEqual(reopened.embeddings_provider, "local")
        self.assertEqual(reopened.embeddings_model, DEFAULT_LOCAL_MODEL)

    def test_semantic_search_works_with_local_vectors(self) -> None:
        with stub_fastembed():
            self.library.build_embeddings(LocalEmbedder())
            query = LocalEmbedder().embed_one("条件概率的定义与性质")
        hits = self.library.search("完全无关的查询", query_vector=query, top_k=2)
        self.assertTrue(hits)

    def test_studybuddy_creates_a_local_embedder_on_demand(self) -> None:
        memory = Memory(self.tmp / "store")
        buddy = StudyBuddy(
            self.tmp / "store",
            model="test",
            api_key="unused",
            library=self.library,
            memory=memory,
            embedding_provider="local",
        )
        with stub_fastembed():
            embedder = buddy.ensure_embedder()
        self.assertIsInstance(embedder, LocalEmbedder)

    def test_build_index_with_local_provider(self) -> None:
        memory = Memory(self.tmp / "store")
        buddy = StudyBuddy(
            self.tmp / "store",
            model="test",
            api_key="unused",
            library=self.library,
            memory=memory,
            embedding_provider="local",
        )
        with stub_fastembed():
            added = buddy.build_index()
        self.assertEqual(added, len(self.library.chunks))
        self.assertEqual(self.library.embeddings_provider, "local")


if __name__ == "__main__":
    unittest.main()
