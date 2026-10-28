"""Regression coverage for upload transport, loopback checks and answer latency."""

from __future__ import annotations

import base64
import json
import threading
from unittest import mock

import studybuddy.web as web_module
from studybuddy import LLMResponse
from studybuddy.web import WebApp, _host_allowed, _origin_allowed
from support import says
from test_study import StudyTestCase, user_prompt
from test_web import WebTestCase
from test_web_security import HttpSecurityTestCase


class TestLoopback(HttpSecurityTestCase):
    def test_domain_prefix_is_not_a_loopback_address(self) -> None:
        for host in ("127.example.invalid", "127.0.0.1.example.invalid", "127.999.1.1", "0.0.0.0"):
            self.assertFalse(_host_allowed(host), host)
            self.assertFalse(_origin_allowed(f"http://{host}"), host)
            self.assertEqual(self.get("/api/state", host=host)[0], 403)
            self.assertEqual(self.post("/api/config", b"{}", origin=f"http://{host}")[0], 403)
        for host in ("127.0.0.2:8765", "[::1]:8765", "localhost:8765"):
            self.assertTrue(_host_allowed(host), host)

    def test_upload_body_can_exceed_normal_json_limit(self) -> None:
        raw = b"x" * (2 * 1024 * 1024)
        body = json.dumps({"name": "book.pdf", "data": base64.b64encode(raw).decode()}).encode()
        with mock.patch.object(self.app, "add_book", return_value={"book": {}}) as upload:
            self.assertEqual(self.post("/api/add-book", body)[0], 200)
            self.assertEqual(base64.b64decode(upload.call_args.args[0]["data"]), raw)
        self.assertEqual(self.post("/api/config", b"", content_length=str(len(body)))[0], 413)

    def test_upload_transport_limit_is_still_enforced(self) -> None:
        with mock.patch.object(web_module, "_MAX_UPLOAD_BODY_BYTES", 1024):
            self.assertEqual(self.post("/api/add-book", b" " * 1025)[0], 413)


class TestCitations(StudyTestCase):
    def test_invalid_book_and_web_numbers_are_removed(self) -> None:
        buddy = self.buddy(says("导数是瞬时变化率 [1]。另一结论 [999][W8]。"))
        answer = buddy.ask("导数", remember=False)
        self.assertIn("[1]", answer.text)
        self.assertNotIn("[999]", answer.text)
        self.assertNotIn("[W8]", answer.text)
        self.assertTrue(any("不存在" in warning for warning in answer.warnings))

    def test_missing_citations_are_reported(self) -> None:
        answer = self.buddy(says("导数是瞬时变化率。")).ask("导数", remember=False)
        self.assertTrue(any("未包含有效" in warning for warning in answer.warnings))

    def test_valid_citations_are_preserved(self) -> None:
        answer = self.buddy(says("导数是瞬时变化率 [1]。")).ask("导数", remember=False)
        self.assertEqual(answer.text, "导数是瞬时变化率 [1]。")
        self.assertEqual(answer.warnings, [])

    def test_disabled_memory_is_not_read(self) -> None:
        self.memory.add_fact("导数薄弱，先看证明", topic="导数")
        buddy = self.buddy(says("导数 [1]"))
        answer = buddy.ask("导数", remember=False)
        self.assertEqual(answer.memories, [])
        self.assertNotIn("导数薄弱，先看证明", user_prompt(buddy.llm))
        self.assertEqual(buddy.llm.request_count, 1)


class TestBackgroundMemory(WebTestCase):
    def test_answer_returns_before_memory_extraction_finishes(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_memory(messages: object) -> LLMResponse:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("memory worker was not released")
            return says('{"topics":["条件概率"],"facts":[],"summary":"学习条件概率"}')

        buddy = self.app(says("条件概率 [1]"), slow_memory).buddy
        app = WebApp(buddy, background_memory=True)
        try:
            data = app.ask("条件概率是什么")
            self.assertIn("[1]", data["text"])
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.memory.episodes, [])
        finally:
            release.set()
            app.close()
        self.assertEqual(len(self.memory.episodes), 1)
        self.assertIn("条件概率", self.memory.topics)

    def test_background_memory_can_be_disabled(self) -> None:
        buddy = self.app(says("条件概率 [1]")).buddy
        app = WebApp(buddy, background_memory=True)
        try:
            status, _ = app.dispatch("POST", "/api/ask", {"question": "条件概率", "remember": False}, {})
            self.assertEqual(status, 200)
        finally:
            app.close()
        self.assertEqual(buddy.llm.request_count, 1)
        self.assertEqual(self.memory.episodes, [])

    def test_clear_discards_inflight_memory(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def delayed(messages: object) -> LLMResponse:
            entered.set()
            release.wait(5)
            return says('{"topics":["条件概率"],"facts":[],"summary":"旧记忆"}')

        app = WebApp(self.app(says("条件概率 [1]"), delayed).buddy, background_memory=True)
        try:
            app.ask("条件概率")
            self.assertTrue(entered.wait(2))
            app.clear_memory()
        finally:
            release.set()
            app.close()
        self.assertEqual(self.memory.episodes, [])
        self.assertEqual(self.memory.topics, {})
