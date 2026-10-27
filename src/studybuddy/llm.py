"""A tiny OpenAI-compatible chat client built on :mod:`urllib`.

The goal is not to cover every provider feature, but to cover the ones an agent
actually needs: tool calling, streaming deltas, retries and timeouts -- with no
third-party dependencies.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

from .errors import LLMError, RetryExhausted
from .types import LLMResponse, Message, ToolCall, Usage

__all__ = ["LLM", "DEFAULT_BASE_URLS"]

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

_API_KEY_ENV_VARS = ("STUDYBUDDY_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _resolve_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in _API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    # Local runtimes such as Ollama ignore the key but some clients still require one.
    return "not-needed"


def _ssl_context(ca_bundle: str | None) -> ssl.SSLContext:
    # create_default_context() honours SSL_CERT_FILE / SSL_CERT_DIR, which is the
    # easiest way to fix "certificate verify failed" on macOS and inside containers.
    return ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()


def _retry_after_seconds(headers: Any, attempt: int, base_delay: float) -> float:
    raw = headers.get("retry-after") if headers is not None else None
    if raw:
        try:
            return min(float(raw), 30.0)
        except ValueError:
            pass
    # Exponential backoff with full jitter keeps many clients from retrying in lockstep.
    ceiling = min(base_delay * (2**attempt), 30.0)
    return random.uniform(0.5 * ceiling, ceiling)


class LLM:
    """A blocking + async OpenAI-compatible chat client.

    Parameters
    ----------
    model:
        Model identifier, e.g. ``"gpt-4o-mini"`` or ``"deepseek-chat"``.
    api_key:
        Falls back to ``STUDYBUDDY_API_KEY``, then the usual provider env vars.
    base_url:
        Any OpenAI-compatible root, e.g. ``https://api.deepseek.com/v1``.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        ca_bundle: str | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = _resolve_api_key(api_key)
        self.base_url = (
            base_url or os.environ.get("STUDYBUDDY_BASE_URL") or DEFAULT_BASE_URLS["openai"]
        ).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.ca_bundle = ca_bundle
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})

    # -- request plumbing ---------------------------------------------------------

    def _headers(self, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "studybuddy/0.1",
        }
        headers.update(self.extra_headers)
        return headers

    def _payload(
        self,
        messages: Sequence[Message] | Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        rendered = [m.to_openai() if isinstance(m, Message) else m for m in messages]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": rendered,
            "temperature": self.temperature,
            "stream": stream,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        if stream:
            # Only ask for usage on the final chunk when the provider supports it.
            payload["stream_options"] = {"include_usage": True}
        payload.update(self.extra_body)
        return payload

    def _open(self, payload: dict[str, Any], stream: bool) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(stream),
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=self.timeout, context=_ssl_context(self.ca_bundle))

    def _error_from(self, exc: urllib.error.HTTPError) -> LLMError:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        message = f"HTTP {exc.code} from {self.base_url}"
        try:
            detail = json.loads(body).get("error", {}).get("message")
            if detail:
                message = f"{message}: {detail}"
        except Exception:
            if body:
                message = f"{message}: {body[:300]}"
        return LLMError(message, status=exc.code, body=body)

    def _with_retries(self, call: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return call()
            except urllib.error.HTTPError as exc:
                error = self._error_from(exc)
                if exc.code not in _RETRYABLE_STATUS:
                    raise error from exc
                last = error
                if attempt == self.max_retries:
                    raise RetryExhausted(attempt + 1, error) from exc
                delay = _retry_after_seconds(exc.headers, attempt, self.retry_base_delay)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt == self.max_retries:
                    raise RetryExhausted(attempt + 1, exc) from exc
                delay = _retry_after_seconds(None, attempt, self.retry_base_delay)
            time.sleep(delay)
        raise RetryExhausted(self.max_retries + 1, last or LLMError("unknown"))

    # -- completion ---------------------------------------------------------------

    def complete(
        self,
        messages: Sequence[Message] | Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """One blocking chat-completions round trip."""
        payload = self._payload(messages, tools, stream=False)

        def call() -> dict[str, Any]:
            with self._open(payload, stream=False) as response:
                parsed: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                return parsed

        data = self._with_retries(call)
        return self._parse(data)

    async def acomplete(
        self,
        messages: Sequence[Message] | Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Async wrapper; the work happens in a worker thread."""
        return await asyncio.to_thread(self.complete, messages, tools)

    @staticmethod
    def _parse_usage(raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        usage = Usage(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
        )
        if not usage.total_tokens:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return usage

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"malformed response: {data!r}"[:500]) from exc

        message = choice.get("message") or {}
        tool_calls = [ToolCall.from_openai(item) for item in message.get("tool_calls") or []]
        return LLMResponse(
            text=message.get("content"),
            tool_calls=tool_calls,
            usage=self._parse_usage(data.get("usage")),
            model=data.get("model"),
            raw=data,
        )

    # -- streaming ----------------------------------------------------------------

    def stream(
        self,
        messages: Sequence[Message] | Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> Iterator[str]:
        """Yield text deltas as they arrive (tool calls are not streamed).

        Use :meth:`complete` when you need tool calls; most agents stream only the
        final answer, which is exactly what this helper is for.
        """
        payload = self._payload(messages, tools, stream=True)
        response = self._with_retries(lambda: self._open(payload, stream=True))
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    return
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content

    async def astream(
        self,
        messages: Sequence[Message] | Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """Async streaming, bridging the blocking generator through a thread."""
        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                for chunk in self.stream(messages, tools):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except Exception as exc:  # noqa: BLE001 - forwarded to the consumer
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        loop.run_in_executor(None, produce)

        while True:
            kind, value = await queue.get()
            if kind == "done":
                return
            if kind == "error":
                raise value
            yield value
