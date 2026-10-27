"""Local, offline embeddings -- no API key, no network after the model is cached.

This is an **optional** extra::

    pip install "studybuddy[local-embed]"

It is deliberately not a default dependency. ``fastembed`` pulls in onnxruntime and
downloads a ~100 MB model on first use, which would turn a three-second install into
a several-minute one for people who only want lexical search.

If the extra is missing, :class:`LocalEmbedder` raises a clear error and the caller
(:class:`~studybuddy.study.StudyBuddy`) falls back to BM25.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from typing import Any

from .errors import StudyError

__all__ = ["LocalEmbedder", "DEFAULT_LOCAL_MODEL", "is_available"]

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
_INSTALL_HINT = 'pip install "studybuddy[local-embed]"'


def is_available() -> bool:
    """True when the optional ``fastembed`` extra is importable."""
    try:
        importlib.import_module("fastembed")
    except ImportError:
        return False
    return True


class LocalEmbedder:
    """Drop-in replacement for :class:`~studybuddy.embed.Embedder` that runs locally.

    Implements the same tiny protocol -- ``model``, ``embed(texts)`` and
    ``embed_one(text)`` -- so the hybrid retrieval code does not care which one it
    gets.
    """

    provider = "local"

    def __init__(
        self,
        model: str = DEFAULT_LOCAL_MODEL,
        *,
        batch_size: int = 32,
        cache_dir: str | None = None,
        threads: int | None = None,
    ) -> None:
        self.model = model
        self.batch_size = max(1, batch_size)
        self.cache_dir = cache_dir
        self.threads = threads
        self._engine: Any = None
        self._dimension: int | None = None

    def __repr__(self) -> str:
        return f"<LocalEmbedder {self.model!r}>"

    def _load(self) -> Any:
        if self._engine is not None:
            return self._engine

        try:
            fastembed = importlib.import_module("fastembed")
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise StudyError(f"本地 embedding 需要可选依赖 fastembed，请先安装: {_INSTALL_HINT}") from exc

        kwargs: dict[str, Any] = {}
        if self.cache_dir:
            kwargs["cache_dir"] = self.cache_dir
        if self.threads:
            kwargs["threads"] = self.threads

        try:
            self._engine = fastembed.TextEmbedding(model_name=self.model, **kwargs)
        except Exception as exc:  # noqa: BLE001 - model name typos, download failures
            raise StudyError(f"无法加载本地 embedding 模型 {self.model!r}: {exc}") from exc
        return self._engine

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def embed(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        """Embed locally, in batches, preserving input order."""
        if not texts:
            return []
        engine = self._load()

        vectors: list[list[float]] = []
        total = len(texts)
        cleaned = [text if text.strip() else " " for text in texts]
        for start in range(0, total, self.batch_size):
            batch = cleaned[start : start + self.batch_size]
            for vector in engine.embed(batch):
                values = [float(value) for value in vector]
                if self._dimension is None:
                    self._dimension = len(values)
                vectors.append(values)
            if on_progress is not None:
                on_progress(min(start + len(batch), total), total)
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
