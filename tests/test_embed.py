"""Embeddings: vectors, fusion, the HTTP client and on-disk storage."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from studybuddy.embed import (
    Embedder,
    EmbeddingIndex,
    cosine,
    pack_vector,
    reciprocal_rank_fusion,
    unpack_vector,
)
from studybuddy.errors import LLMError
from support import FakeResponse


class TestCosine(unittest.TestCase):
    def test_identical_vectors(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0)

    def test_orthogonal_vectors(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_opposite_vectors(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_zero_vector_is_safe(self) -> None:
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_dimension_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            cosine([1.0], [1.0, 2.0])


class TestPacking(unittest.TestCase):
    def test_round_trip(self) -> None:
        vector = [0.5, -1.25, 3.0, 0.0]
        self.assertEqual(unpack_vector(pack_vector(vector)), vector)

    def test_pack_is_compact(self) -> None:
        packed = pack_vector([0.123456] * 1536)
        self.assertLess(len(packed), 1536 * 4 * 2)  # base64 overhead only

    def test_empty(self) -> None:
        self.assertEqual(unpack_vector(pack_vector([])), [])


class TestFusion(unittest.TestCase):
    def test_document_in_both_rankings_wins(self) -> None:
        fused = reciprocal_rank_fusion(
            [
                ["a", "b", "c"],
                ["b", "c", "a"],
            ]
        )
        self.assertEqual(fused[0][0], "b")

    def test_document_from_one_ranking_is_kept(self) -> None:
        fused = dict(reciprocal_rank_fusion([["a"], ["b"]]))
        self.assertIn("b", fused)

    def test_order_is_descending(self) -> None:
        fused = reciprocal_rank_fusion([["a", "b", "c"]])
        scores = [score for _item, score in fused]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty(self) -> None:
        self.assertEqual(reciprocal_rank_fusion([[], []]), [])


def embedding_response(count: int, dim: int = 3, *, reverse: bool = False) -> str:
    data = [{"index": index, "embedding": [float(index + 1)] * dim} for index in range(count)]
    if reverse:
        data.reverse()
    return json.dumps({"data": data, "model": "fake-embed"})


class TestEmbedderClient(unittest.TestCase):
    def test_request_shape(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["request"] = request
            captured["body"] = json.loads(request.data)
            return FakeResponse(embedding_response(2))

        with patch("studybuddy.embed.urllib.request.urlopen", side_effect=fake_urlopen):
            embedder = Embedder("fake-embed", api_key="secret", base_url="https://api.example.test/v1")
            vectors = embedder.embed(["hello", "world"])

        self.assertEqual(vectors, [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]])
        self.assertEqual(captured["request"].full_url, "https://api.example.test/v1/embeddings")
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer secret")
        self.assertEqual(captured["body"]["model"], "fake-embed")
        self.assertEqual(captured["body"]["input"], ["hello", "world"])

    def test_dimensions_are_sent_when_set(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["body"] = json.loads(request.data)
            return FakeResponse(embedding_response(1))

        with patch("studybuddy.embed.urllib.request.urlopen", side_effect=fake_urlopen):
            Embedder("m", api_key="k", dimensions=512).embed(["x"])

        self.assertEqual(captured["body"]["dimensions"], 512)

    def test_results_are_reordered_by_index(self) -> None:
        with patch(
            "studybuddy.embed.urllib.request.urlopen",
            return_value=FakeResponse(embedding_response(3, reverse=True)),
        ):
            vectors = Embedder("m", api_key="k").embed(["a", "b", "c"])

        self.assertEqual([vector[0] for vector in vectors], [1.0, 2.0, 3.0])

    def test_batching(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout=None, context=None):
            body = json.loads(request.data)
            calls.append(len(body["input"]))
            return FakeResponse(embedding_response(len(body["input"])))

        with patch("studybuddy.embed.urllib.request.urlopen", side_effect=fake_urlopen):
            vectors = Embedder("m", api_key="k", batch_size=2).embed(["a", "b", "c", "d", "e"])

        self.assertEqual(calls, [2, 2, 1])
        self.assertEqual(len(vectors), 5)

    def test_blank_text_is_not_dropped(self) -> None:
        seen: list[str] = []

        def fake_urlopen(request, timeout=None, context=None):
            body = json.loads(request.data)
            seen.extend(body["input"])
            return FakeResponse(embedding_response(len(body["input"])))

        with patch("studybuddy.embed.urllib.request.urlopen", side_effect=fake_urlopen):
            Embedder("m", api_key="k").embed(["", "  ", "x"])

        self.assertEqual(len(seen), 3)

    def test_retries_then_succeeds(self) -> None:
        error = urllib.error.HTTPError("https://x", 503, "boom", {}, io.BytesIO(b"{}"))
        with (
            patch(
                "studybuddy.embed.urllib.request.urlopen",
                side_effect=[error, FakeResponse(embedding_response(1))],
            ),
            patch("studybuddy.embed.time.sleep"),
        ):
            vectors = Embedder("m", api_key="k", max_retries=1).embed(["x"])
        self.assertEqual(len(vectors), 1)

    def test_client_error_is_not_retried(self) -> None:
        error = urllib.error.HTTPError("https://x", 401, "nope", {}, io.BytesIO(b'{"error":"bad key"}'))
        with (
            patch("studybuddy.embed.urllib.request.urlopen", side_effect=error),
            patch("studybuddy.embed.time.sleep"),
            self.assertRaises(LLMError),
        ):
            Embedder("m", api_key="k", max_retries=3).embed(["x"])

    def test_malformed_response(self) -> None:
        with (
            patch("studybuddy.embed.urllib.request.urlopen", return_value=FakeResponse('{"data": []}')),
            self.assertRaises(LLMError),
        ):
            Embedder("m", api_key="k").embed(["x"])

    def test_embed_one(self) -> None:
        with patch(
            "studybuddy.embed.urllib.request.urlopen", return_value=FakeResponse(embedding_response(1, dim=2))
        ):
            self.assertEqual(Embedder("m", api_key="k").embed_one("x"), [1.0, 1.0])


class TestEmbeddingIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.index = EmbeddingIndex(
            "fake-embed",
            ["a", "b", "c"],
            [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]],
        )

    def test_search_orders_by_similarity(self) -> None:
        results = self.index.search([1.0, 0.0], k=3)
        self.assertEqual([chunk_id for chunk_id, _score in results], ["a", "c", "b"])

    def test_allowed_filter(self) -> None:
        results = self.index.search([1.0, 0.0], k=3, allowed={"b"})
        self.assertEqual([chunk_id for chunk_id, _score in results], ["b"])

    def test_dimension_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            self.index.search([1.0, 0.0, 3.0], k=1)

    def test_zero_query(self) -> None:
        self.assertEqual(self.index.search([0.0, 0.0], k=3), [])

    def test_vector_for(self) -> None:
        self.assertEqual(self.index.vector_for("b"), [0.0, 1.0])
        self.assertIsNone(self.index.vector_for("zzz"))

    def test_covers(self) -> None:
        self.assertTrue(self.index.covers(["a", "b"]))
        self.assertFalse(self.index.covers(["a", "zzz"]))

    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.jsonl"
            self.index.save(path)
            loaded = EmbeddingIndex.load(path)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.model, "fake-embed")
        self.assertEqual(loaded.chunk_ids, ["a", "b", "c"])
        restored = loaded.vector_for("c")
        assert restored is not None
        self.assertAlmostEqual(restored[0], 0.9, places=5)
        self.assertAlmostEqual(restored[1], 0.1, places=5)
        self.assertEqual(loaded.dimension, 2)

    def test_load_missing_file(self) -> None:
        self.assertIsNone(EmbeddingIndex.load("/tmp/definitely-not-here-12345.jsonl"))

    def test_load_corrupted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text("not json at all\n", encoding="utf-8")
            self.assertIsNone(EmbeddingIndex.load(path))

    def test_load_without_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nometa.jsonl"
            path.write_text(
                json.dumps({"chunk_id": "a", "vec": pack_vector([1.0, 0.0])}) + "\n", encoding="utf-8"
            )
            self.assertIsNone(EmbeddingIndex.load(path))


if __name__ == "__main__":
    unittest.main()
