"""Lexical retrieval tuned for Chinese textbooks, with no tokenizer dependency.

Chinese has no spaces, so a whitespace tokenizer retrieves nothing useful. Instead
of pulling in a segmentation library, CJK runs are indexed as **character bigrams**:
"导数与微分" becomes 导数/数与/与微/微分. This is the classic n-gram trick -- it is
fully offline, language-agnostic and works well on the formal prose textbooks use.

Latin text is indexed as lower-cased word tokens, so "derivative" and "Derivatives"
match.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence

__all__ = ["tokenize", "BM25"]

_LATIN_TOKEN = re.compile(r"[a-z0-9]+(?:[._'’-][a-z0-9]+)*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def tokenize(text: str) -> list[str]:
    """Split text into index terms: Latin words plus CJK character bigrams."""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens = _LATIN_TOKEN.findall(normalized)
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


class BM25:
    """Okapi BM25 over a fixed list of documents.

    The corpus is small (a semester of textbooks), so everything is computed in
    memory and rebuilt when the library changes.
    """

    def __init__(self, documents: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.documents = [list(tokens) for tokens in documents]
        self.size = len(self.documents)
        self.term_freqs = [Counter(tokens) for tokens in self.documents]
        self.lengths = [len(tokens) for tokens in self.documents]
        self.average_length = (sum(self.lengths) / self.size) if self.size else 0.0

        document_freq: Counter[str] = Counter()
        for tokens in self.documents:
            document_freq.update(set(tokens))
        self.idf: dict[str, float] = {
            term: math.log(1.0 + (self.size - freq + 0.5) / (freq + 0.5))
            for term, freq in document_freq.items()
        }

    def scores(self, query: str | Iterable[str]) -> list[float]:
        """Compute the BM25 score of every document against the query."""
        query_terms = tokenize(query) if isinstance(query, str) else list(query)
        if not query_terms or not self.size:
            return [0.0] * self.size

        unique_terms = list(dict.fromkeys(query_terms))
        average = self.average_length or 1.0
        results = [0.0] * self.size

        for term in unique_terms:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for index, freqs in enumerate(self.term_freqs):
                frequency = freqs.get(term)
                if not frequency:
                    continue
                length_ratio = self.lengths[index] / average
                denominator = frequency + self.k1 * (1.0 - self.b + self.b * length_ratio)
                results[index] += idf * frequency * (self.k1 + 1.0) / denominator
        return results

    def top_k(
        self,
        query: str | Iterable[str],
        k: int = 6,
        *,
        allowed: set[int] | None = None,
        min_score: float = 0.0,
    ) -> list[tuple[int, float]]:
        """Return ``(document_index, score)`` for the best matches, best first."""
        scored = self.scores(query)
        candidates = (
            (index, score) for index, score in enumerate(scored) if allowed is None or index in allowed
        )
        ranked = sorted(
            ((index, score) for index, score in candidates if score > min_score),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:k]
