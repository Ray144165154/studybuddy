"""Security and robustness tests for the local web UI.

Every test here asserts a *protective* behaviour that was added after an
independent review found the 0.6.1 transport layer was exploitable:

* arbitrary file read through ``..`` in the static path
* arbitrary file write through the upload filename
* cross-site requests (CSRF) and DNS rebinding
* unbounded request bodies, unbounded questions
* internal paths and exception details leaking through error responses

Run with::

    PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests \\
        -p "test_web_security.py" -v
"""

from __future__ import annotations

import base64
import contextlib
import inspect
import json
import socket
import tempfile
import threading
import time
import traceback
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

import studybuddy.web as web_module
from studybuddy import Book, Chunk, Library, LLMResponse, Memory, StudyBuddy, Usage
from studybuddy.web import _STATIC_DIR, WebApp, _Handler, serve
from support import ScriptedLLM, make_textbook

MEMORY_JSON = '{"topics": ["条件概率"], "facts": [], "summary": "问了条件概率"}'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def parse_http(raw: bytes) -> tuple[int | None, dict[str, str], bytes]:
    """Split a raw HTTP/1.x response into (status, headers, body)."""
    if not raw:
        return None, {}, b""
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return None, {}, raw
    lines = head.split(b"\r\n")
    if not lines or not lines[0].startswith(b"HTTP/"):
        return None, {}, raw
    try:
        status = int(lines[0].split(b" ")[1])
    except (IndexError, ValueError):
        return None, {}, raw
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return status, headers, body


def json_body(body: bytes) -> dict[str, Any]:
    payload = json.loads(body.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


class _QuietServer(ThreadingHTTPServer):
    """A server that records handler exceptions instead of printing tracebacks."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.errors: list[str] = []

    def handle_error(self, request: Any, client_address: Any) -> None:
        self.errors.append(traceback.format_exc())


class SlowLockLLM:
    """Two calls per ask; records how many overlap in time."""

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._mu = threading.Lock()

    def complete(self, messages: Any, tools: Any = None) -> LLMResponse:
        with self._mu:
            self.active += 1
            self.calls += 1
            call_number = self.calls
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._mu:
            self.active -= 1
        if call_number % 2 == 1:
            return LLMResponse(text="答 [1]", usage=Usage(total_tokens=7), model="slow")
        return LLMResponse(text=MEMORY_JSON, usage=Usage(total_tokens=7), model="slow")

    async def acomplete(self, messages: Any, tools: Any = None) -> LLMResponse:
        return self.complete(messages, tools)


class SecurityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "data"
        self.library = Library(self.root)
        self.library.books = [Book(id="m1", title="概率论基础", course="概率论", pages=10, chunks=1)]
        self.library.chunks = [
            Chunk(
                id="m1:p10:0",
                book_id="m1",
                book_title="概率论基础",
                page=10,
                section="1.1 条件概率",
                text="条件概率 P(A|B) 是在事件 B 发生的条件下事件 A 发生的概率。",
            )
        ]
        self.library.save()
        self.memory = Memory(self.root)

    def make_app(self, llm: Any = None) -> WebApp:
        buddy = StudyBuddy(
            self.root,
            model="test-model",
            api_key="not-needed",
            llm=llm if llm is not None else ScriptedLLM(),
            library=self.library,
            memory=self.memory,
        )
        return WebApp(buddy)


class HttpSecurityTestCase(SecurityTestCase):
    """Starts a real server so raw sockets can send un-normalised paths."""

    def setUp(self) -> None:
        super().setUp()
        self.app = self.make_app(ScriptedLLM())
        handler = type("Handler", (_Handler,), {"app": self.app})
        self.server = _QuietServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def raw(self, request: bytes, *, timeout: float = 3.0) -> tuple[int | None, dict[str, str], bytes]:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.sendall(request)
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                try:
                    data = sock.recv(65536)
                except (TimeoutError, ConnectionResetError):
                    break
                if not data:
                    break
                chunks.append(data)
        return parse_http(b"".join(chunks))

    def get(self, path: str, *, host: str | None = None) -> tuple[int | None, dict[str, str], bytes]:
        request = f"GET {path} HTTP/1.1\r\n"
        request += f"Host: {host or f'127.0.0.1:{self.port}'}\r\n"
        request += "Connection: close\r\n\r\n"
        return self.raw(request.encode())

    def post(
        self,
        path: str,
        body: bytes = b"{}",
        *,
        content_type: str = "application/json",
        host: str | None = None,
        origin: str | None = None,
        content_length: str | None = None,
    ) -> tuple[int | None, dict[str, str], bytes]:
        request = f"POST {path} HTTP/1.1\r\n"
        request += f"Host: {host or f'127.0.0.1:{self.port}'}\r\n"
        request += f"Content-Type: {content_type}\r\n"
        if origin is not None:
            request += f"Origin: {origin}\r\n"
        length = str(len(body)) if content_length is None else content_length
        request += f"Content-Length: {length}\r\n"
        request += "Connection: close\r\n\r\n"
        return self.raw(request.encode() + body)


# ---------------------------------------------------------------------------
# static files
# ---------------------------------------------------------------------------


class TestStaticFiles(HttpSecurityTestCase):
    def test_serves_index_and_assets(self) -> None:
        for path, expected in (("/", "text/html"), ("/style.css", "text/css"), ("/app.js", "javascript")):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, headers.get("content-type", ""))
            self.assertTrue(body)

    def test_unknown_static_path_is_json_404(self) -> None:
        status, headers, body = self.get("/nope.js")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertIn("error", json_body(body))

    def test_parent_directory_traversal_is_blocked(self) -> None:
        for hops in ("../", "../../", "../../../", "../../../../../../../../../../"):
            status, _headers, body = self.get("/" + hops + "pyproject.toml")
            self.assertEqual(status, 404, f"path escaped with {hops!r}")
            self.assertNotIn(b"[project]", body)

    def test_absolute_traversal_is_blocked(self) -> None:
        hops = "../" * (len(_STATIC_DIR.parts) - 1)
        status, _headers, body = self.get("/" + hops + "etc/passwd")
        self.assertEqual(status, 404)
        self.assertNotIn(b"root:", body)

    def test_percent_encoded_traversal_is_blocked(self) -> None:
        for path in ("/%2e%2e/pyproject.toml", "/%2e%2e%2fpyproject.toml", "/%2E%2E/pyproject.toml"):
            status, _headers, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"[project]", body)

    def test_static_responses_are_nosniff(self) -> None:
        _status, headers, _body = self.get("/")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")


# ---------------------------------------------------------------------------
# request framing
# ---------------------------------------------------------------------------


class TestRequestFraming(HttpSecurityTestCase):
    def test_invalid_content_length_is_rejected(self) -> None:
        status, _headers, body = self.post("/api/config", b"{}", content_length="abc")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", json_body(body)["error"])

    def test_negative_content_length_is_rejected(self) -> None:
        status, _headers, body = self.post("/api/config", b"{}", content_length="-5")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", json_body(body)["error"])

    def test_missing_content_length_is_rejected(self) -> None:
        request = (
            f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n\r\n"
        )
        status, _headers, _body = self.raw(request.encode())
        self.assertEqual(status, 411)

    def test_oversized_body_is_rejected(self) -> None:
        with mock.patch.object(web_module, "_MAX_BODY_BYTES", 1024):
            status, _headers, body = self.post("/api/config", b" " * 1025)
        self.assertEqual(status, 413)
        self.assertIn("过大", json_body(body)["error"])

    def test_body_at_the_limit_is_accepted(self) -> None:
        payload = b'{"model":"m"}'
        padded = payload + b" " * (1024 - len(payload))
        with mock.patch.object(web_module, "_MAX_BODY_BYTES", 1024):
            status, _headers, _body = self.post("/api/config", padded)
        self.assertEqual(status, 200)

    def test_non_json_body_is_rejected(self) -> None:
        status, _headers, body = self.post("/api/config", b"hello")
        self.assertEqual(status, 400)
        self.assertIn("JSON", json_body(body)["error"])

    def test_json_array_body_is_rejected(self) -> None:
        status, _headers, body = self.post("/api/config", b"[1,2,3]")
        self.assertEqual(status, 400)
        self.assertIn("对象", json_body(body)["error"])


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


class TestUploads(SecurityTestCase):
    def upload(self, name: Any, data: Any, *, app: WebApp | None = None) -> tuple[int, dict[str, Any]]:
        return (app or self.make_app()).dispatch("POST", "/api/add-book", {"name": name, "data": data}, {})

    def pdf_b64(self) -> str:
        return base64.b64encode(make_textbook(self.tmp / "payload.pdf").read_bytes()).decode()

    def test_valid_upload_is_indexed(self) -> None:
        status, data = self.upload("book.pdf", self.pdf_b64())
        self.assertEqual(status, 200)
        self.assertGreater(data["book"]["chunks"], 0)
        self.assertTrue((self.root / "uploads" / "book.pdf").is_file())

    def test_relative_filename_traversal_is_rejected(self) -> None:
        status, data = self.upload("../escaped.pdf", self.pdf_b64())
        self.assertEqual(status, 400)
        self.assertIn("文件名", data["error"])
        self.assertFalse((self.root / "escaped.pdf").exists())

    def test_absolute_filename_is_rejected(self) -> None:
        target = self.tmp / "escaped_absolute.pdf"
        status, data = self.upload(str(target), self.pdf_b64())
        self.assertEqual(status, 400)
        self.assertIn("文件名", data["error"])
        self.assertFalse(target.exists())

    def test_nul_byte_in_filename_is_rejected(self) -> None:
        status, _data = self.upload("evil\x00.pdf", self.pdf_b64())
        self.assertEqual(status, 400)

    def test_dot_names_are_rejected(self) -> None:
        for name in (".", ".."):
            status, _data = self.upload(name, self.pdf_b64())
            self.assertEqual(status, 400, name)

    def test_invalid_base64_is_rejected_before_writing(self) -> None:
        status, data = self.upload("x.pdf", "!!!not base64!!!")
        self.assertEqual(status, 400)
        self.assertIn("base64", data["error"])
        self.assertFalse((self.root / "uploads" / "x.pdf").exists())

    def test_missing_data_is_rejected(self) -> None:
        for data in (None, "", 123, ["a"]):
            status, _payload = self.upload("x.pdf", data)
            self.assertEqual(status, 400, data)

    def test_malformed_pdf_is_400_and_cleans_up(self) -> None:
        encoded = base64.b64encode(b"not a pdf at all").decode()
        status, data = self.upload("bad.pdf", encoded)
        self.assertEqual(status, 400)
        self.assertNotIn("Traceback", json.dumps(data))
        self.assertFalse((self.root / "uploads" / "bad.pdf").exists(), "temp file leaked")

    def test_upload_limit_is_enforced_after_decoding(self) -> None:
        encoded = base64.b64encode(b"x" * 2048).decode()
        with mock.patch.object(web_module, "_MAX_UPLOAD_BYTES", 1024):
            status, data = self.upload("big.pdf", encoded)
        self.assertEqual(status, 400)
        self.assertIn("太大", data["error"])


# ---------------------------------------------------------------------------
# error disclosure and status mapping
# ---------------------------------------------------------------------------


class TestErrors(SecurityTestCase):
    def test_quiz_bad_count_is_400(self) -> None:
        status, data = self.make_app().dispatch("POST", "/api/quiz", {"count": "abc"}, {})
        self.assertEqual(status, 400)
        self.assertIn("count", data["error"])

    def test_review_bad_limit_is_400(self) -> None:
        status, data = self.make_app().dispatch("GET", "/api/review", None, {"limit": ["abc"]})
        self.assertEqual(status, 400)

    def test_internal_error_is_generic(self) -> None:
        class Exploding:
            def complete(self, messages: Any, tools: Any = None) -> LLMResponse:
                raise RuntimeError("secret /internal/path")

            async def acomplete(self, messages: Any, tools: Any = None) -> LLMResponse:
                return self.complete(messages, tools)

        status, data = self.make_app(Exploding()).dispatch(
            "POST", "/api/ask", {"question": "什么是条件概率"}, {}
        )
        self.assertEqual(status, 500)
        blob = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("secret", blob)
        self.assertNotIn("/internal/path", blob)
        self.assertNotIn("RuntimeError", blob)
        self.assertIn("内部错误", blob)

    def test_llm_error_is_502(self) -> None:
        class Failing:
            def complete(self, messages: Any, tools: Any = None) -> LLMResponse:
                from studybuddy.errors import LLMError

                raise LLMError("upstream 500")

            async def acomplete(self, messages: Any, tools: Any = None) -> LLMResponse:
                return self.complete(messages, tools)

        status, data = self.make_app(Failing()).dispatch("POST", "/api/ask", {"question": "q"}, {})
        self.assertEqual(status, 502)
        self.assertIn("模型调用失败", data["error"])


# ---------------------------------------------------------------------------
# CSRF / DNS rebinding
# ---------------------------------------------------------------------------


class TestCrossSite(HttpSecurityTestCase):
    def test_text_plain_post_is_rejected(self) -> None:
        status, _headers, body = self.post("/api/config", b'{"model":"evil"}', content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertIn("application/json", json_body(body)["error"])

    def test_cross_origin_post_is_rejected(self) -> None:
        status, _headers, body = self.post("/api/config", b'{"model":"evil"}', origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertIn("origin", json_body(body)["error"])

    def test_null_origin_is_rejected(self) -> None:
        status, _headers, _body = self.post("/api/config", b"{}", origin="null")
        self.assertEqual(status, 403)

    def test_loopback_origin_is_allowed(self) -> None:
        origin = f"http://127.0.0.1:{self.port}"
        status, _headers, _body = self.post("/api/config", b'{"model":"ok"}', origin=origin)
        self.assertEqual(status, 200)

    def test_non_loopback_host_is_rejected(self) -> None:
        for method in ("GET", "POST"):
            if method == "GET":
                status, _headers, body = self.get("/api/state", host="evil.example")
            else:
                status, _headers, body = self.post("/api/config", b"{}", host="evil.example")
            self.assertEqual(status, 403, method)
            self.assertIn("host", json_body(body)["error"])

    def test_loopback_host_variants_are_allowed(self) -> None:
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"):
            status, _headers, _body = self.get("/api/state", host=host)
            self.assertEqual(status, 200, host)


# ---------------------------------------------------------------------------
# methods, limits, configure
# ---------------------------------------------------------------------------


class TestMethodsAndLimits(HttpSecurityTestCase):
    def test_unsupported_methods_are_json_405(self) -> None:
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            request = (
                f"{method} /api/config HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
            )
            status, headers, body = self.raw(request.encode())
            self.assertEqual(status, 405, method)
            self.assertIn("application/json", headers.get("content-type", ""))
            self.assertIn("error", json_body(body))

    def test_overlong_question_is_rejected(self) -> None:
        app = self.make_app(ScriptedLLM())
        status, data = app.dispatch("POST", "/api/ask", {"question": "啊" * 9000}, {})
        self.assertEqual(status, 400)
        self.assertIn("太长", data["error"])


class TestConfigure(SecurityTestCase):
    def test_non_string_values_are_rejected(self) -> None:
        for field in ("model", "base_url", "api_key"):
            status, data = self.make_app().dispatch("POST", "/api/config", {field: ["x"]}, {})
            self.assertEqual(status, 400, field)
            self.assertIn(field, data["error"])

    def test_empty_payload_is_a_noop(self) -> None:
        status, data = self.make_app().dispatch("POST", "/api/config", {}, {})
        self.assertEqual(status, 200)
        self.assertFalse(data["llm_ready"])

    def test_api_key_is_never_written_to_disk(self) -> None:
        app = self.make_app()
        status, _data = app.dispatch("POST", "/api/config", {"api_key": "sk-canary-123"}, {})
        self.assertEqual(status, 200)
        self.assertTrue(app.llm_ready)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"sk-canary-123", path.read_bytes(), f"key leaked into {path}")


# ---------------------------------------------------------------------------
# concurrency and safety defaults
# ---------------------------------------------------------------------------


class TestConcurrency(SecurityTestCase):
    def test_ask_is_serialized(self) -> None:
        llm = SlowLockLLM(delay=0.05)
        app = self.make_app(llm)
        results: list[int] = []

        def worker() -> None:
            status, _data = app.dispatch("POST", "/api/ask", {"question": "什么是条件概率"}, {})
            results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(results, [200, 200, 200])
        self.assertEqual(llm.max_active, 1, "ask() must not overlap")


class TestSafetyDefaults(unittest.TestCase):
    def test_serve_binds_loopback_by_default(self) -> None:
        signature = inspect.signature(serve)
        self.assertEqual(signature.parameters["host"].default, "127.0.0.1")

    def test_body_and_upload_limits_exist(self) -> None:
        self.assertLessEqual(web_module._MAX_BODY_BYTES, 4 * 1024 * 1024)
        self.assertLessEqual(web_module._MAX_UPLOAD_BYTES, 100 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
