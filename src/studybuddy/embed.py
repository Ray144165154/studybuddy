"""Optional semantic retrieval: embeddings, vector storage and rank fusion.

Lexical BM25 is excellent at terminology and terrible at paraphrases: a student who
asks "为什么求导和微分是一回事" may never match a page that only says "导数与微分的关系".
Embeddings close that gap.

This module is deliberately optional. If no embedding endpoint is configured the
library keeps working exactly as before -- `None` in, BM25 out.

Fusion uses **Reciprocal Rank Fusion** rather than a weighted sum of scores: BM25
scores and cosine similarities live on incomparable scales, and RRF only needs the
ranks, which makes it both simpler and harder to misconfigure.
"""

from __future__ import annotations

import array
import base64
import contextlib
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import LLMError
from .llm import _RETRYABLE_STATUS, _resolve_api_key, _retry_after_seconds, _ssl_context

__all__ = [
    "Embedder",
    "EmbeddingIndex",
    "cosine",
    "reciprocal_rank_fusion",
    "pack_vector",
    "unpack_vector",
    "DEFAULT_EMBEDDING_MODEL",
]

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_RRF_K = 60


# --------------------------------------------------------------------------------------
# vectors
# --------------------------------------------------------------------------------------


def pack_vector(vector: Sequence[float]) -> str:
    """float32 + base64: 4 bytes per dimension instead of ~20 for JSON numbers."""
    floats = array.array("f", vector)
    if sys.byteorder != "little":  # pragma: no cover - all CI targets are little-endian
        floats.byteswap()
    return base64.b64encode(floats.tobytes()).decode("ascii")


def unpack_vector(packed: str) -> list[float]:
    raw = base64.b64decode(packed)
    floats = array.array("f")
    floats.frombytes(raw)
    if sys.byteorder != "little":  # pragma: no cover
        floats.byteswap()
    return floats.tolist()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, returning 0.0 for a zero vector instead of dividing by it."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b, strict=False):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one ranking.

    ``score(d) = sum over rankings of 1 / (k + rank(d))``. A document that both
    retrievers like wins; a document only one of them found still gets a fair shot.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


# --------------------------------------------------------------------------------------
# embedding client
# --------------------------------------------------------------------------------------


class Embedder:
    """A client for any OpenAI-compatible ``/embeddings`` endpoint.

    Works with OpenAI, Ollama, vLLM, LM Studio and friends. Providers that do not
    expose embeddings (DeepSeek at the time of writing) simply do not configure it.
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        dimensions: int | None = None,
        timeout: float = 60.0,
        batch_size: int = 64,
        max_retries: int = 2,
        retry_base_delay: float = 0.5,
        ca_bundle: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        import os

        self.model = model
        self.api_key = _resolve_api_key(api_key)
        self.base_url = (
            base_url
            or os.environ.get("STUDYBUDDY_EMBED_BASE_URL")
            or os.environ.get("STUDYBUDDY_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.dimensions = dimensions
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.ca_bundle = ca_bundle
        self.extra_headers = dict(extra_headers or {})

    def __repr__(self) -> str:
        return f"<Embedder {self.model!r} at {self.base_url}>"

    # -- HTTP ----------------------------------------------------------------------

    def _payload(self, texts: Sequence[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "input": list(texts)}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "studybuddy/0.2",
        }
        headers.update(self.extra_headers)
        return headers

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(self._payload(texts)).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )

        def call() -> dict[str, Any]:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=_ssl_context(self.ca_bundle)
            ) as response:
                parsed: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                return parsed

        data = self._with_retries(call)
        entries = data.get("data")
        if not isinstance(entries, list) or len(entries) != len(texts):
            raise LLMError(f"malformed embeddings response: expected {len(texts)} vectors")

        ordered = sorted(entries, key=lambda item: int(item.get("index", 0)))
        return [[float(value) for value in entry["embedding"]] for entry in ordered]

    def _with_retries(self, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                return call()
            except urllib.error.HTTPError as exc:
                body = ""
                with contextlib.suppress(Exception):
                    body = exc.read().decode("utf-8", "replace")[:300]
                if exc.code not in _RETRYABLE_STATUS or attempt == self.max_retries:
                    raise LLMError(
                        f"embeddings request failed (HTTP {exc.code}): {body}", status=exc.code
                    ) from exc
                delay = _retry_after_seconds(exc.headers, attempt, self.retry_base_delay)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == self.max_retries:
                    raise LLMError(f"embeddings request failed: {exc}") from exc
                delay = _retry_after_seconds(None, attempt, self.retry_base_delay)
            time.sleep(delay + random.uniform(0.0, 0.1))
        raise LLMError("embeddings request failed")  # pragma: no cover

    # -- public API ----------------------------------------------------------------

    def embed(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        """Embed every text, in batches, preserving the input order."""
        cleaned = [text if text.strip() else " " for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(cleaned), self.batch_size):
            batch = cleaned[start : start + self.batch_size]
            vectors.extend(self._post(batch))
            if on_progress is not None:
                on_progress(min(start + len(batch), len(cleaned)), len(cleaned))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# --------------------------------------------------------------------------------------
# vector index
# --------------------------------------------------------------------------------------


class EmbeddingIndex:
    """Vectors for a set of chunks, persisted as JSONL of base64 float32."""

    def __init__(
        self,
        model: str,
        chunk_ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        *,
        created_at: str = "",
    ) -> None:
        if len(chunk_ids) != len(vectors):
            raise ValueError("chunk_ids and vectors must be the same length")
        self.model = model
        self.chunk_ids = list(chunk_ids)
        self.vectors = [array.array("f", vector) for vector in vectors]
        self.dimension = len(self.vectors[0]) if self.vectors else 0
        self.created_at = created_at or datetime.now().isoformat(timespec="seconds")
        self._norms = [self._norm(vector) for vector in self.vectors]
        self._positions = {chunk_id: index for index, chunk_id in enumerate(self.chunk_ids)}

    @staticmethod
    def _norm(vector: array.array[float]) -> float:
        total = 0.0
        for value in vector:
            total += value * value
        return math.sqrt(total)

    def __len__(self) -> int:
        return len(self.chunk_ids)

    def __contains__(self, chunk_id: object) -> bool:
        return chunk_id in self._positions

    def vector_for(self, chunk_id: str) -> list[float] | None:
        """The stored vector for a chunk, or ``None`` when it was never embedded."""
        position = self._positions.get(chunk_id)
        if position is None:
            return None
        return list(self.vectors[position])

    def covers(self, chunk_ids: Iterable[str]) -> bool:
        """True when every requested chunk already has a vector."""
        return all(chunk_id in self._positions for chunk_id in chunk_ids)

    def search(
        self,
        query_vector: Sequence[float],
        *,
        k: int = 20,
        allowed: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``(chunk_id, cosine_similarity)`` for the closest chunks."""
        if not self.vectors:
            return []
        if len(query_vector) != self.dimension:
            raise ValueError(f"query has {len(query_vector)} dimensions, index has {self.dimension}")

        query_norm = 0.0
        for value in query_vector:
            query_norm += value * value
        query_norm = math.sqrt(query_norm)
        if query_norm <= 0.0:
            return []

        scored: list[tuple[str, float]] = []
        for position, vector in enumerate(self.vectors):
            chunk_id = self.chunk_ids[position]
            if allowed is not None and chunk_id not in allowed:
                continue
            norm = self._norms[position]
            if norm <= 0.0:
                continue
            dot = 0.0
            for left, right in zip(vector, query_vector, strict=False):
                dot += left * right
            scored.append((chunk_id, dot / (norm * query_norm)))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:k]

    # -- persistence ---------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            meta = {
                "kind": "meta",
                "model": self.model,
                "dimension": self.dimension,
                "chunks": len(self.chunk_ids),
                "created_at": self.created_at,
            }
            handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for chunk_id, vector in zip(self.chunk_ids, self.vectors, strict=False):
                handle.write(json.dumps({"chunk_id": chunk_id, "vec": pack_vector(vector)}) + "\n")
        temporary.replace(target)

    @classmethod
    def load(cls, path: str | Path) -> EmbeddingIndex | None:
        """Load an index, or ``None`` when the file is absent or malformed."""
        target = Path(path)
        if not target.exists():
            return None

        model = ""
        chunk_ids: list[str] = []
        vectors: list[list[float]] = []
        created_at = ""
        try:
            with target.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("kind") == "meta":
                        model = str(entry.get("model") or "")
                        created_at = str(entry.get("created_at") or "")
                        continue
                    chunk_ids.append(str(entry["chunk_id"]))
                    vectors.append(unpack_vector(entry["vec"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

        if not model or not chunk_ids:
            return None
        return cls(model, chunk_ids, vectors, created_at=created_at)
