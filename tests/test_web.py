"""The local web UI: API behaviour, safety defaults and one real HTTP smoke test."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from studybuddy import Book, Chunk, Library, Memory, StudyBuddy
from studybuddy.web import WebApp, _Handler
from support import ScriptedLLM, make_textbook, says

MEMORY_JSON = '{"topics": ["条件概率"], "facts": [], "summary": "问了条件概率"}'


class WebTestCase(unittest.TestCase):
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

    def app(self, *responses) -> WebApp:
        buddy = StudyBuddy(
            self.root,
            model="test-model",
            api_key="not-needed",
            llm=ScriptedLLM(*responses),
            library=self.library,
            memory=self.memory,
        )
        return WebApp(buddy)


class TestState(WebTestCase):
    def test_state_shape(self) -> None:
        status, data = self.app().dispatch("GET", "/api/state", None, {})
        self.assertEqual(status, 200)
        self.assertEqual(data["stats"]["chunks"], 1)
        self.assertEqual(data["books"][0]["title"], "概率论基础")
        self.assertFalse(data["llm_ready"])
        self.assertIn("root", data)

    def test_memory_payload(self) -> None:
        self.memory.add_fact("导数薄弱", kind="weakness", topic="导数")
        self.memory.note_topic("极限")
        status, data = self.app().dispatch("GET", "/api/memory", None, {})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["facts"]), 1)
        self.assertEqual(len(data["topics"]), 1)

    def test_suggestions_come_from_section_titles(self) -> None:
        self.library.chunks = [
            Chunk(
                id="m1:p1:0",
                book_id="m1",
                book_title="概率论基础",
                page=1,
                section="3.1 条件概率",
                text="条件概率的定义。",
            ),
            Chunk(
                id="m1:p2:0",
                book_id="m1",
                book_title="概率论基础",
                page=2,
                section="7.2 大数定律",
                text="大数定律。",
            ),
        ]
        self.library.save()
        _, data = self.app().dispatch("GET", "/api/state", None, {})
        self.assertEqual(len(data["suggestions"]), 2)
        self.assertIn("条件概率", data["suggestions"][0])

    def test_suggestions_ignore_ocr_noise(self) -> None:
        self.library.chunks = [
            Chunk(id="m1:p1:0", book_id="m1", book_title="书", page=1, section="第1章 概率与测度", text="x"),
            Chunk(
                id="m1:p2:0", book_id="m1", book_title="书", page=2, section="第1章 概率与測度", text="x"
            ),  # 近重复
            Chunk(
                id="m1:p3:0", book_id="m1", book_title="书", page=3, section="51.2 事件与集合", text="x"
            ),  # OCR 把 1.2 认成 51.2
            Chunk(id="m1:p4:0", book_id="m1", book_title="书", page=4, section="1.2.2 事件的运算", text="x"),
        ]
        self.library.save()
        _, data = self.app().dispatch("GET", "/api/state", None, {})
        self.assertEqual(
            data["suggestions"],
            ["第1章 概率与测度 讲了什么？", "1.2.2 事件的运算 讲了什么？"],
        )

    def test_no_suggestions_without_books(self) -> None:
        self.library.chunks = []
        self.library.save()
        _, data = self.app().dispatch("GET", "/api/state", None, {})
        self.assertEqual(data["suggestions"], [])

    def test_state_reports_writable(self) -> None:
        _, data = self.app().dispatch("GET", "/api/state", None, {})
        self.assertTrue(data["writable"])

    def test_state_reports_read_only(self) -> None:
        app = self.app()
        with patch("pathlib.Path.write_text", side_effect=OSError("read-only")):
            self.assertFalse(app.library_writable())

    def test_unknown_endpoint(self) -> None:
        status, data = self.app().dispatch("GET", "/api/nope", None, {})
        self.assertEqual(status, 404)
        self.assertIn("error", data)


class TestAsk(WebTestCase):
    def test_ask_returns_answer_and_sources(self) -> None:
        app = self.app(says("条件概率的定义是…… [1]"), says(MEMORY_JSON))
        status, data = app.dispatch("POST", "/api/ask", {"question": "什么是条件概率？"}, {})
        self.assertEqual(status, 200)
        self.assertIn("条件概率", data["text"])
        self.assertEqual(data["sources"][0]["page"], 10)
        self.assertEqual(data["sources"][0]["book"], "概率论基础")
        self.assertIn("usage", data)

    def test_missing_model_is_explained(self) -> None:
        from studybuddy import LLM

        buddy = StudyBuddy(
            self.root,
            model="deepseek-chat",
            api_key="",
            llm=LLM(model="deepseek-chat", api_key="", base_url="http://127.0.0.1:9/v1"),
            library=self.library,
            memory=self.memory,
        )
        if getattr(buddy.llm, "api_key", "") not in ("", "not-needed"):
            self.skipTest("environment provides an API key")
        status, data = WebApp(buddy).dispatch("POST", "/api/ask", {"question": "什么是条件概率"}, {})
        self.assertEqual(status, 400)
        self.assertIn("设置", data["error"])

    def test_empty_question_is_rejected(self) -> None:
        status, data = self.app().dispatch("POST", "/api/ask", {"question": "  "}, {})
        self.assertEqual(status, 400)
        self.assertIn("空", data["error"])

    def test_empty_library_is_explained(self) -> None:
        self.library.chunks = []
        self.library.save()
        status, data = self.app().dispatch("POST", "/api/ask", {"question": "q"}, {})
        self.assertEqual(status, 400)
        self.assertIn("书库是空的", data["error"])

    def test_internal_failure_does_not_leak_details(self) -> None:
        class Broken(ScriptedLLM):
            def complete(self, messages, tools=None):  # type: ignore[no-untyped-def]
                raise RuntimeError("upstream down at /secret/path")

        buddy = StudyBuddy(
            self.root, model="m", api_key="k", llm=Broken(), library=self.library, memory=self.memory
        )
        status, data = WebApp(buddy).dispatch("POST", "/api/ask", {"question": "q"}, {})
        self.assertEqual(status, 500)
        self.assertNotIn("upstream down", data["error"])
        self.assertNotIn("/secret/path", data["error"])


class TestBooksAndUpload(WebTestCase):
    def test_add_text(self) -> None:
        status, data = self.app().dispatch(
            "POST", "/api/add-text", {"title": "笔记", "text": "条件概率的定义。"}, {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["book"]["title"], "笔记")
        self.assertEqual(len(self.library.books), 2)

    def test_add_text_requires_title(self) -> None:
        status, data = self.app().dispatch("POST", "/api/add-text", {"text": "x"}, {})
        self.assertEqual(status, 400)

    def test_upload_pdf(self) -> None:
        pdf = make_textbook(self.tmp / "book.pdf")
        encoded = base64.b64encode(pdf.read_bytes()).decode()
        status, data = self.app().dispatch(
            "POST", "/api/add-book", {"name": "book.pdf", "data": encoded, "course": "概率论"}, {}
        )
        self.assertEqual(status, 200)
        self.assertGreater(data["book"]["chunks"], 0)
        self.assertTrue((self.root / "uploads" / "book.pdf").exists())

    def test_upload_rejects_bad_base64(self) -> None:
        status, data = self.app().dispatch("POST", "/api/add-book", {"name": "x.pdf", "data": "!!!"}, {})
        self.assertEqual(status, 400)
        self.assertIn("base64", data["error"])

    def test_upload_requires_data(self) -> None:
        status, _data = self.app().dispatch("POST", "/api/add-book", {"name": "x.pdf"}, {})
        self.assertEqual(status, 400)


class TestSettings(WebTestCase):
    def test_configure_updates_process_only(self) -> None:
        app = self.app()
        status, data = app.dispatch(
            "POST",
            "/api/config",
            {"model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test"},
            {},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["llm_ready"])
        self.assertEqual(app.buddy.model, "deepseek-chat")

        # The key must not be written anywhere under the data root.
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"sk-test", path.read_bytes(), f"key leaked into {path}")

    def test_configure_without_key_stays_unready(self) -> None:
        app = self.app()
        _, data = app.dispatch("POST", "/api/config", {"model": "m2"}, {})
        self.assertFalse(data["llm_ready"])


class TestPersistedSettings(WebTestCase):
    def test_model_and_endpoint_are_saved_but_not_the_key(self) -> None:
        app = self.app()
        app.dispatch(
            "POST",
            "/api/config",
            {"model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-secret"},
            {},
        )
        raw = (self.root / "ui-config.json").read_text("utf-8")
        saved = json.loads(raw)
        self.assertEqual(saved["model"], "deepseek-chat")
        self.assertEqual(saved["base_url"], "https://api.deepseek.com/v1")
        self.assertNotIn("sk-secret", raw)

    def test_saved_settings_are_restored_on_restart(self) -> None:
        self.app().dispatch(
            "POST",
            "/api/config",
            {"model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1"},
            {},
        )

        fresh = StudyBuddy(
            self.root,
            model="gpt-4o-mini",
            api_key="not-needed",
            llm=ScriptedLLM(),
            library=self.library,
            memory=self.memory,
        )
        WebApp(fresh)  # construction restores the saved choice

        self.assertEqual(fresh.model, "deepseek-chat")
        self.assertEqual(getattr(fresh.llm, "base_url", ""), "https://api.deepseek.com/v1")
        self.assertFalse(WebApp(fresh).llm_ready, "the key must not be restored")

    def test_read_only_data_dir_does_not_break_settings(self) -> None:
        app = self.app()
        with patch("pathlib.Path.write_text", side_effect=OSError("read-only")):
            status, data = app.dispatch(
                "POST",
                "/api/config",
                {"model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1"},
                {},
            )
        self.assertEqual(status, 200)
        self.assertEqual(data["model"], "deepseek-chat")
        self.assertEqual(app.buddy.model, "deepseek-chat")

    def test_missing_config_file_is_fine(self) -> None:
        app = self.app()
        self.assertEqual(app.buddy.model, "test-model")


class TestMemoryActions(WebTestCase):
    def test_clear_memory(self) -> None:
        self.memory.add_fact("x")
        self.memory.note_topic("极限")
        status, _ = self.app().dispatch("POST", "/api/memory/clear", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(self.memory.facts, [])
        self.assertEqual(self.memory.topics, {})

    def test_grade(self) -> None:
        self.memory.note_topic("极限")
        status, data = self.app().dispatch("POST", "/api/grade", {"topic": "极限", "correct": True}, {})
        self.assertEqual(status, 200)
        self.assertEqual(data["mastery"], 1.0)

    def test_profile_update(self) -> None:
        status, data = self.app().dispatch(
            "POST", "/api/profile", {"major": "数学", "courses": ["概率论"]}, {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["profile"]["major"], "数学")

    def test_review_empty(self) -> None:
        status, data = self.app().dispatch("GET", "/api/review", None, {"limit": ["3"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])


class TestHttpServer(WebTestCase):
    """One end-to-end check over a real socket."""

    def setUp(self) -> None:
        super().setUp()
        self.app = self.app(says("答案 [1]"), says(MEMORY_JSON))
        handler = type("Handler", (_Handler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def get(self, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_serves_the_page(self) -> None:
        status, body, content_type = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"studybuddy", body)

    def test_serves_assets(self) -> None:
        for asset in ("/style.css", "/app.js"):
            status, body, _ = self.get(asset)
            self.assertEqual(status, 200)
            self.assertTrue(body)

    def test_state_over_http(self) -> None:
        status, body, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stats"]["chunks"], 1)

    def test_ask_over_http(self) -> None:
        status, data = self.post("/api/ask", {"question": "什么是条件概率？"})
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], "答案 [1]")


if __name__ == "__main__":
    unittest.main()
