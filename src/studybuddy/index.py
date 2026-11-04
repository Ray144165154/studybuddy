"""Lexical retrieval tuned for Chinese textbooks, with no tokenizer dependency.

Chinese has no spaces, so a whitespace tokenizer retrieves nothing useful. Instead
of pulling in a segmentation library, CJK runs are indexed as **character bigrams**
*and* single characters: "导数与微分" becomes 导数/数与/与微/微分 plus 导/数/与/微/分.
The bigrams carry the precision. The single characters exist only so that a
one-character term ("熵") or a term at the edge of a question ("什么是熵" -- the
bigram 是熵 never occurs in prose that starts "熵是…") can be found at all. They are
kept out of the main ranking and only used as a fallback when no bigram matches.

Latin text is indexed as lower-cased word tokens, so "derivative" and
"Derivatives" match.
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
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Function characters carry no topic on their own. They are dropped from the
# character fallback so "为什么 AB 不等于 BA" does not match every page that
# happens to contain 不 or 是. Bigrams still cover compounds such as 不等式.
_UNIGRAM_STOPWORDS = frozenset(
    "的得了着过是在有和与及或而则即故因虽但且并且也又再还只才很更最"
    "可能够会要应所之其此何如若为以于由从向对把被让使给跟比用不等"
    "什怎么吗呢吧啊呀哦嗯这那哪些个们我你他她它上下中内外前后时该"
)


def tokenize(text: str) -> list[str]:
    """Split text into index terms: Latin words, CJK bigrams and single CJK characters."""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens = _LATIN_TOKEN.findall(normalized)
    for run in _CJK_RUN.findall(normalized):
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        tokens.extend(run)
    return tokens


def _is_cjk_unigram(token: str) -> bool:
    """True for a single Chinese character (the fallback tokens, not the bigrams)."""
    return len(token) == 1 and _CJK_CHAR.match(token) is not None


def _idf(term_freqs: Sequence[Counter[str]], size: int) -> dict[str, float]:
    """Inverse document frequency over ``Counter``-per-document term frequencies."""
    if size <= 0:
        return {}
    document_freq: Counter[str] = Counter()
    for freqs in term_freqs:
        document_freq.update(freqs.keys())
    return {term: math.log(1.0 + (size - freq + 0.5) / (freq + 0.5)) for term, freq in document_freq.items()}


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

        # Single CJK characters are a recall fallback, not part of the main index.
        # Keeping them out of the length statistics means adding them cannot change
        # any BM25 score that already worked.
        self.term_freqs = [
            Counter(token for token in tokens if not _is_cjk_unigram(token)) for tokens in self.documents
        ]
        self.lengths = [sum(freqs.values()) for freqs in self.term_freqs]
        self.average_length = (sum(self.lengths) / self.size) if self.size else 0.0
        self.idf = _idf(self.term_freqs, self.size)

        self._unigram_freqs = [
            Counter(token for token in tokens if _is_cjk_unigram(token)) for tokens in self.documents
        ]
        self._unigram_lengths = [sum(freqs.values()) for freqs in self._unigram_freqs]
        self._unigram_average = (sum(self._unigram_lengths) / self.size) if self.size else 0.0
        self._unigram_idf = _idf(self._unigram_freqs, self.size)
        self._unigram_terms = [frozenset(freqs.keys()) for freqs in self._unigram_freqs]

    def scores(self, query: str | Iterable[str]) -> list[float]:
        """Compute the BM25 score of every document against the query."""
        query_terms = tokenize(query) if isinstance(query, str) else list(query)
        if not query_terms or not self.size:
            return [0.0] * self.size

        results = self._score_terms(query_terms, self.term_freqs, self.lengths, self.average_length, self.idf)
        if isinstance(query, str) and not any(results):
            results = self._fallback_scores(query_terms)
        return results

    def _score_terms(
        self,
        terms: Sequence[str],
        term_freqs: Sequence[Counter[str]],
        lengths: Sequence[int],
        average_length: float,
        idf: dict[str, float],
    ) -> list[float]:
        unique_terms = list(dict.fromkeys(terms))
        average = average_length or 1.0
        results = [0.0] * self.size

        for term in unique_terms:
            term_idf = idf.get(term)
            if term_idf is None:
                continue
            for index, freqs in enumerate(term_freqs):
                frequency = freqs.get(term)
                if not frequency:
                    continue
                length_ratio = lengths[index] / average
                denominator = frequency + self.k1 * (1.0 - self.b + self.b * length_ratio)
                results[index] += term_idf * frequency * (self.k1 + 1.0) / denominator
        return results

    def _fallback_scores(self, query_terms: Sequence[str]) -> list[float]:
        """Score character by character when no bigram matched anything.

        Only topic characters are used, and a passage must contain *every* one of
        them: this is the last resort that makes "熵" and "什么是熵" work, not a
        licence to match a whole page on a single common character.
        """
        terms = list(
            dict.fromkeys(
                term for term in query_terms if _is_cjk_unigram(term) and term not in _UNIGRAM_STOPWORDS
            )
        )
        if not terms:
            return [0.0] * self.size
        results = self._score_terms(
            terms, self._unigram_freqs, self._unigram_lengths, self._unigram_average, self._unigram_idf
        )
        for index, score in enumerate(results):
            if score and not all(term in self._unigram_terms[index] for term in terms):
                results[index] = 0.0
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
