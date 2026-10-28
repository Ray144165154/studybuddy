"""Optional web search, used when the textbooks do not cover the question.

Textbook grounding is the product's promise, so search is always a *second* source:
the prompt tells the model to prefer the uploaded pages and to cite web results as
``[W1]`` so a reader can tell the two apart.

Providers, in order of reliability:

``tavily``      Tavily API -- built for LLM agents, free tier. Needs a key.
``serper``      Google results via serper.dev. Needs a key.
``duckduckgo``  No key, scrapes the HTML endpoint. Works out of the box but may be
                rate-limited or blocked on some networks.

Selection: ``STUDYBUDDY_SEARCH_PROVIDER`` if set, otherwise the first provider with
a configured key, otherwise DuckDuckGo.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import AgentError
from .llm import _ssl_context

__all__ = [
    "SearchResult",
    "WebSearch",
    "DuckDuckGoSearch",
    "TavilySearch",
    "SerperSearch",
    "build_search",
]

_TIMEOUT = 15.0
_USER_AGENT = "Mozilla/5.0 (compatible; studybuddy/1.0; +https://github.com/Ray144165154/studybuddy)"


class SearchError(AgentError):
    """The search provider failed or returned something unusable."""


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class WebSearch(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]: ...


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT, context=_ssl_context(None)) as response:
            data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", "replace")[:200]
        raise SearchError(f"{url} 返回 HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SearchError(f"无法连接搜索服务 {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SearchError(f"搜索服务返回了非 JSON 内容: {exc}") from exc


class TavilySearch:
    name = "tavily"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise SearchError("Tavily 需要 API Key（STUDYBUDDY_SEARCH_API_KEY）")
        self.api_key = api_key

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max(1, min(limit, 10)),
            "search_depth": "basic",
            "include_answer": False,
        }
        data = _post_json("https://api.tavily.com/search", payload, {})
        results = data.get("results") or []
        return [
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("content") or "").strip()[:500],
            )
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]


class SerperSearch:
    name = "serper"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise SearchError("Serper 需要 API Key（STUDYBUDDY_SEARCH_API_KEY）")
        self.api_key = api_key

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        data = _post_json(
            "https://google.serper.dev/search",
            {"q": query, "num": max(1, min(limit, 10))},
            {"X-API-KEY": self.api_key},
        )
        results = data.get("organic") or []
        return [
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("link") or "").strip(),
                snippet=str(item.get("snippet") or "").strip()[:500],
            )
            for item in results
            if isinstance(item, dict) and item.get("link")
        ]


_RESULT_BLOCK = re.compile(
    r'<div class="result results_links.*?(?=<div class="result results_links|</body>)', re.S
)
_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAGS = re.compile(r"<[^>]+>")


def _strip_tags(value: str) -> str:
    return html.unescape(_TAGS.sub("", value)).strip()


def _unwrap_redirect(url: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<target>."""
    if "uddg=" not in url:
        return url
    parsed = urllib.parse.urlparse(url if url.startswith("http") else "https:" + url)
    target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
    return urllib.parse.unquote(target) or url


class DuckDuckGoSearch:
    """No-key fallback. Scrapes the HTML endpoint, so it can break or be blocked."""

    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        body = urllib.parse.urlencode({"q": query, "kl": "cn-zh"}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT, context=_ssl_context(None)) as response:
                page = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise SearchError(f"DuckDuckGo 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SearchError(f"无法连接 DuckDuckGo: {exc}") from exc

        return self.parse(page, limit=limit)

    @staticmethod
    def parse(page: str, *, limit: int = 5) -> list[SearchResult]:
        results: list[SearchResult] = []
        for block in _RESULT_BLOCK.findall(page):
            link = _LINK.search(block)
            if not link:
                continue
            url = _unwrap_redirect(html.unescape(link.group(1)))
            if not url.startswith("http"):
                continue
            snippet = _SNIPPET.search(block)
            results.append(
                SearchResult(
                    title=_strip_tags(link.group(2)),
                    url=url,
                    snippet=_strip_tags(snippet.group(1)) if snippet else "",
                )
            )
            if len(results) >= limit:
                break
        return results


def _env_key(provider: str) -> str | None:
    specific = os.environ.get(f"STUDYBUDDY_{provider.upper()}_API_KEY")
    return specific or os.environ.get("STUDYBUDDY_SEARCH_API_KEY") or None


def build_search(provider: str | None = None) -> WebSearch | None:
    """Pick a provider from config/environment. Returns ``None`` when none is usable."""
    requested = (provider or os.environ.get("STUDYBUDDY_SEARCH_PROVIDER") or "").strip().lower()

    if requested in ("", "auto"):
        for candidate in ("tavily", "serper"):
            key = _env_key(candidate)
            if key:
                return TavilySearch(key) if candidate == "tavily" else SerperSearch(key)
        return DuckDuckGoSearch()

    if requested == "tavily":
        return TavilySearch(_env_key("tavily") or "")
    if requested == "serper":
        return SerperSearch(_env_key("serper") or "")
    if requested in ("duckduckgo", "ddg"):
        return DuckDuckGoSearch()
    if requested == "none":
        return None
    raise SearchError(f"未知的搜索服务: {requested}")
