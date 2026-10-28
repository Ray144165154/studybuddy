"""Web search: provider selection, HTML parsing, and how it enters the prompt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studybuddy import Library, Memory, StudyBuddy
from studybuddy.search import (
    DuckDuckGoSearch,
    SearchError,
    SearchResult,
    TavilySearch,
    build_search,
)
from support import ScriptedLLM, says

MEMORY_JSON = '{"topics": [], "facts": [], "summary": "s"}'

DDG_PAGE = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fzh.wikipedia.org%2Fwiki%2F%E4%B8%AD%E5%BF%83%E6%9E%81%E9%99%90%E5%AE%9A%E7%90%86&amp;rut=x">
      中心极限定理 - 维基百科
    </a>
  </h2>
  <a class="result__snippet" href="#">中心极限定理是概率论中的一组定理。</a>
</div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://baike.baidu.com/item/CLT">中心极限定理_百度百科</a>
  </h2>
  <a class="result__snippet" href="#">描述独立同分布随机变量。</a>
</div>
</body></html>
"""


class FakeSearch:
    name = "fake"

    def __init__(self, results: list[SearchResult] | None = None, error: str | None = None) -> None:
        self.results = results or []
        self.error = error
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        if self.error:
            raise SearchError(self.error)
        return self.results[:limit]


class TestDuckDuckGoParsing(unittest.TestCase):
    def test_parses_titles_urls_and_snippets(self) -> None:
        results = DuckDuckGoSearch.parse(DDG_PAGE, limit=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "中心极限定理 - 维基百科")
        self.assertEqual(results[0].url, "https://zh.wikipedia.org/wiki/中心极限定理")
        self.assertIn("概率论", results[0].snippet)
        self.assertEqual(results[1].url, "https://baike.baidu.com/item/CLT")

    def test_limit_is_respected(self) -> None:
        self.assertEqual(len(DuckDuckGoSearch.parse(DDG_PAGE, limit=1)), 1)

    def test_empty_page(self) -> None:
        self.assertEqual(DuckDuckGoSearch.parse("<html></html>", limit=3), [])


class TestProviderSelection(unittest.TestCase):
    def test_tavily_key_wins_when_auto(self) -> None:
        with patch.dict("os.environ", {"STUDYBUDDY_TAVILY_API_KEY": "tvly-x"}, clear=True):
            self.assertIsInstance(build_search(), TavilySearch)

    def test_serper_key_when_no_tavily(self) -> None:
        with patch.dict("os.environ", {"STUDYBUDDY_SERPER_API_KEY": "s-x"}, clear=True):
            self.assertEqual(build_search().name, "serper")

    def test_falls_back_to_duckduckgo(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(build_search().name, "duckduckgo")

    def test_explicit_none_disables(self) -> None:
        with patch.dict("os.environ", {"STUDYBUDDY_SEARCH_PROVIDER": "none"}, clear=True):
            self.assertIsNone(build_search())

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(SearchError):
            build_search("bing")

    def test_tavily_without_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(SearchError):
            TavilySearch("")


class WebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "data"
        self.library = Library(self.root)
        self.library.add_document(
            title="概率论", pages=[(1, "中心极限定理：独立同分布，方差有限，则标准化和依分布收敛于正态。")]
        )
        self.memory = Memory(self.root)


class TestWebInPrompt(WebTestCase):
    def buddy(self, *responses, search=None) -> StudyBuddy:
        return StudyBuddy(
            self.root,
            model="test",
            api_key="unused",
            llm=ScriptedLLM(*responses),
            library=self.library,
            memory=self.memory,
            search_client=search,
        )

    def test_web_results_are_added_to_the_prompt(self) -> None:
        search = FakeSearch(
            [SearchResult(title="维基", url="https://zh.wikipedia.org/wiki/CLT", snippet="摘要")]
        )
        llm = ScriptedLLM(says("答 [1][W1]"), says(MEMORY_JSON))
        buddy = StudyBuddy(
            self.root,
            model="t",
            api_key="u",
            llm=llm,
            library=self.library,
            memory=self.memory,
            search_client=search,
        )
        answer = buddy.ask("中心极限定理是什么", web=True)

        prompt = llm.calls[0][-1].content
        self.assertIn("【网络搜索结果】", prompt)
        self.assertIn("[W1]", prompt)
        self.assertIn("zh.wikipedia.org", prompt)
        self.assertEqual(answer.web_sources[0].url, "https://zh.wikipedia.org/wiki/CLT")
        self.assertIn("网络来源", answer.with_citations())

    def test_web_is_off_by_default(self) -> None:
        search = FakeSearch([SearchResult(title="t", url="https://example.com")])
        buddy = self.buddy(says("答 [1]"), says(MEMORY_JSON), search=search)
        answer = buddy.ask("中心极限定理")
        self.assertEqual(search.queries, [], "search must not run unless asked")
        self.assertEqual(answer.web_sources, [])

    def test_search_failure_is_a_warning_not_an_error(self) -> None:
        search = FakeSearch(error="网络不可达")
        buddy = self.buddy(says("答 [1]"), says(MEMORY_JSON), search=search)
        answer = buddy.ask("中心极限定理", web=True)
        self.assertEqual(answer.text, "答 [1]")
        self.assertTrue(any("联网搜索失败" in warning for warning in answer.warnings))
        self.assertEqual(answer.web_sources, [])

    def test_unconfigured_provider_warns(self) -> None:
        with patch.dict("os.environ", {"STUDYBUDDY_SEARCH_PROVIDER": "none"}, clear=True):
            buddy = self.buddy(says("答 [1]"), says(MEMORY_JSON))
            answer = buddy.ask("中心极限定理", web=True)
        self.assertTrue(any("搜索" in warning for warning in answer.warnings))
        self.assertEqual(answer.web_sources, [])

    def test_web_sources_are_not_labelled_as_textbook_citations(self) -> None:
        search = FakeSearch([SearchResult(title="t", url="https://example.com/a")])
        answer = self.buddy(says("答 [1][W1]"), says(MEMORY_JSON), search=search).ask(
            "中心极限定理", web=True
        )
        rendered = answer.with_citations()
        self.assertIn("教材来源:", rendered)
        self.assertIn("网络来源:", rendered)
        self.assertIn("[W1]", rendered)


if __name__ == "__main__":
    unittest.main()
