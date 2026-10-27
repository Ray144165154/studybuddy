"""HTTP layer: payload shape, parsing, retries, error mapping and streaming."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from studybuddy import LLM
from studybuddy.errors import LLMError, RetryExhausted
from studybuddy.types import Message
from support import FakeResponse


def chat_body(text: str = "hi", *, tool_calls=None, usage=None) -> str:
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
    )


def http_error(code: int, message: str = "boom") -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError(
        "https://example.test/v1/chat/completions", code, message, {}, io.BytesIO(body)
    )


class TestPayload(unittest.TestCase):
    def test_request_shape_and_auth_header(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["request"] = request
            captured["body"] = json.loads(request.data)
            return FakeResponse(chat_body())

        llm = LLM(model="m1", api_key="secret", base_url="https://api.example.test/v1", temperature=0.3)
        with patch("studybuddy.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            llm.complete([Message(role="user", content="hello")])

        request = captured["request"]
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(captured["body"]["model"], "m1")
        self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(captured["body"]["temperature"], 0.3)
        self.assertFalse(captured["body"]["stream"])
        self.assertNotIn("tools", captured["body"])

    def test_tools_are_forwarded(self) -> None:
        captured: dict = {}
        schema = {"type": "function", "function": {"name": "f", "description": "d", "parameters": {}}}

        def fake_urlopen(request, timeout=None, context=None):
            captured["body"] = json.loads(request.data)
            return FakeResponse(chat_body())

        with patch("studybuddy.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            LLM(api_key="k").complete([Message(role="user", content="x")], [schema])

        self.assertEqual(captured["body"]["tools"], [schema])
        self.assertEqual(captured["body"]["tool_choice"], "auto")

    def test_api_key_from_environment(self) -> None:
        with patch.dict(os.environ, {"STUDYBUDDY_API_KEY": "env-key"}, clear=True):
            self.assertEqual(LLM().api_key, "env-key")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True):
            self.assertEqual(LLM().api_key, "openai-key")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(LLM().api_key, "not-needed")


class TestParsing(unittest.TestCase):
    def _complete(self, body: str):
        with patch("studybuddy.llm.urllib.request.urlopen", return_value=FakeResponse(body)):
            return LLM(api_key="k").complete([Message(role="user", content="x")])

    def test_plain_text(self) -> None:
        response = self._complete(chat_body("hello there"))
        self.assertEqual(response.text, "hello there")
        self.assertEqual(response.tool_calls, [])
        self.assertEqual(response.usage.total_tokens, 7)
        self.assertEqual(response.model, "test-model")

    def test_tool_calls_are_parsed(self) -> None:
        body = chat_body(
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
                }
            ]
        )
        response = self._complete(body)
        self.assertIsNone(response.text)
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual((call.id, call.name), ("call_abc", "add"))
        self.assertEqual(call.arguments, {"a": 1, "b": 2})

    def test_malformed_json_arguments_are_preserved(self) -> None:
        body = chat_body(
            tool_calls=[{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{not json"}}]
        )
        call = self._complete(body).tool_calls[0]
        self.assertEqual(call.arguments, {"__raw_arguments__": "{not json"})

    def test_malformed_envelope_raises(self) -> None:
        with self.assertRaises(LLMError):
            self._complete(json.dumps({"nope": True}))

    def test_usage_falls_back_to_sum(self) -> None:
        body = json.dumps(
            {
                "choices": [{"message": {"content": "x"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            }
        )
        self.assertEqual(self._complete(body).usage.total_tokens, 5)


class TestRetries(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep = patch("studybuddy.llm.time.sleep").start()
        self.addCleanup(patch.stopall)

    def test_retries_then_succeeds(self) -> None:
        side_effect = [http_error(503), FakeResponse(chat_body("recovered"))]
        with patch("studybuddy.llm.urllib.request.urlopen", side_effect=side_effect):
            response = LLM(api_key="k", max_retries=2).complete([Message(role="user", content="x")])
        self.assertEqual(response.text, "recovered")
        self.assertEqual(self.sleep.call_count, 1)

    def test_retry_exhausted(self) -> None:
        with (
            patch("studybuddy.llm.urllib.request.urlopen", side_effect=[http_error(500)] * 3),
            self.assertRaises(RetryExhausted),
        ):
            LLM(api_key="k", max_retries=2).complete([Message(role="user", content="x")])

    def test_client_error_is_not_retried(self) -> None:
        with (
            patch("studybuddy.llm.urllib.request.urlopen", side_effect=http_error(401, "bad key")),
            self.assertRaises(LLMError) as ctx,
        ):
            LLM(api_key="k", max_retries=3).complete([Message(role="user", content="x")])
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("bad key", str(ctx.exception))
        self.assertEqual(self.sleep.call_count, 0)

    def test_network_error_is_retried(self) -> None:
        side_effect = [urllib.error.URLError("offline"), FakeResponse(chat_body("ok"))]
        with patch("studybuddy.llm.urllib.request.urlopen", side_effect=side_effect):
            response = LLM(api_key="k", max_retries=1).complete([Message(role="user", content="x")])
        self.assertEqual(response.text, "ok")

    def test_network_error_exhausted(self) -> None:
        with (
            patch("studybuddy.llm.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")),
            self.assertRaises(RetryExhausted),
        ):
            LLM(api_key="k", max_retries=1).complete([Message(role="user", content="x")])


def sse(*chunks: str) -> str:
    lines = []
    for chunk in chunks:
        delta = {"choices": [{"delta": {"content": chunk}}]}
        lines.append(f"data: {json.dumps(delta)}\n")
        lines.append("\n")
    lines.append("data: [DONE]\n")
    return "".join(lines)


class TestStreaming(unittest.TestCase):
    def test_stream_yields_deltas(self) -> None:
        with patch("studybuddy.llm.urllib.request.urlopen", return_value=FakeResponse(sse("Hel", "lo", "!"))):
            chunks = list(LLM(api_key="k").stream([Message(role="user", content="x")]))
        self.assertEqual("".join(chunks), "Hello!")

    def test_stream_ignores_noise(self) -> None:
        body = ': keep-alive\n\ndata: {"choices":[]}\n\ndata: [DONE]\n'
        with patch("studybuddy.llm.urllib.request.urlopen", return_value=FakeResponse(body)):
            self.assertEqual(list(LLM(api_key="k").stream([Message(role="user", content="x")])), [])


if __name__ == "__main__":
    unittest.main()
