"""A small local web UI, served by the standard library.

Design constraints, in order of importance:

1. **No new dependencies.** ``http.server`` plus a static HTML/CSS/JS bundle.
2. **Local by default.** Binds to 127.0.0.1; the API key lives in process memory
   only and is never written to disk.
3. **Thin. It calls :class:`StudyBuddy` and returns JSON -- no business logic here.

The transport is deliberately boring: JSON in, JSON out. Uploads are base64 in a
JSON body so multipart parsing (and its bugs) never enters the codebase.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .errors import LLMError, StudyError
from .study import StudyBuddy

__all__ = ["WebApp", "serve"]

_STATIC_DIR = Path(__file__).parent / "webui"
_MAX_UPLOAD_BYTES = 80 * 1024 * 1024
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


class StudyErrorBadRequest(Exception):
    """Raised for bad user input; mapped to HTTP 400."""


class WebApp:
    """The API surface the single page app talks to."""

    def __init__(self, buddy: StudyBuddy) -> None:
        self.buddy = buddy
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------------

    @property
    def llm_ready(self) -> bool:
        key = getattr(self.buddy.llm, "api_key", "")
        return bool(key) and key != "not-needed"

    def state(self) -> dict[str, Any]:
        library = self.buddy.library
        return {
            "version": __version__,
            "model": self.buddy.model,
            "base_url": getattr(self.buddy.llm, "base_url", ""),
            "root": str(library.root),
            "llm_ready": self.llm_ready,
            "books": [book.to_dict() for book in library.books],
            "stats": {
                "books": len(library.books),
                "pages": sum(book.pages for book in library.books),
                "chunks": len(library.chunks),
            },
            "memory": self.buddy.memory.stats(),
            "profile": self.buddy.memory.profile.to_dict(),
            "has_embeddings": library.has_embeddings,
        }

    def memory_payload(self) -> dict[str, Any]:
        memory = self.buddy.memory
        return {
            "profile": memory.profile.to_dict(),
            "profile_summary": memory.profile.summary(),
            "facts": [fact.to_dict() for fact in memory.facts[-20:]],
            "topics": [
                topic.to_dict() | {"mastery": topic.mastery}
                for topic in sorted(memory.topics.values(), key=lambda item: item.seen, reverse=True)[:20]
            ],
            "episodes": [episode.to_dict() for episode in memory.episodes[-10:]],
            "due": [topic.name for topic in memory.due_topics(limit=10)],
        }

    # -- actions -------------------------------------------------------------------

    def ask(self, question: str, *, rewrite: bool = False, semantic: bool = True) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            raise StudyErrorBadRequest("问题是空的")
        if not self.buddy.library.chunks:
            raise StudyError("书库是空的 -- 先在「书库」里导入一本教材")

        with self._lock:
            answer = self.buddy.ask(question, rewrite=rewrite, semantic=semantic)

        return {
            "text": answer.text,
            "sources": [
                {
                    "index": source.index,
                    "book": source.chunk.book_title,
                    "page": source.chunk.page,
                    "section": source.chunk.section,
                    "snippet": source.chunk.text[:160],
                    "unreliable": source.chunk.unreliable,
                }
                for source in answer.sources
            ],
            "memories": answer.memories,
            "warnings": answer.warnings,
            "usage": {
                "prompt_tokens": answer.usage.prompt_tokens,
                "completion_tokens": answer.usage.completion_tokens,
                "total_tokens": answer.usage.total_tokens,
            },
        }

    def add_book(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "教材.pdf")
        encoded = payload.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise StudyErrorBadRequest("没有收到文件内容")

        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StudyErrorBadRequest(f"文件内容不是有效的 base64: {exc}") from exc
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise StudyErrorBadRequest("文件太大（上限 80MB）")

        temporary = Path(self.buddy.root) / "uploads" / name
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(raw)

        with self._lock:
            book = self.buddy.add_book(
                temporary,
                course=str(payload.get("course") or ""),
                title=str(payload.get("title") or "") or None,
            )
        return {"book": book.to_dict()}

    def add_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        text = str(payload.get("text") or "")
        if not title:
            raise StudyErrorBadRequest("缺少书名")
        if not text.strip():
            raise StudyErrorBadRequest("没有收到文本内容")
        with self._lock:
            book = self.buddy.add_document(
                title=title,
                pages=[(1, text)],
                course=str(payload.get("course") or ""),
            )
        return {"book": book.to_dict()}

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update the model settings for this process only (the key is never stored)."""
        if "model" in payload and str(payload["model"]).strip():
            self.buddy.model = str(payload["model"]).strip()
            self.buddy.llm.model = self.buddy.model
        if "base_url" in payload and str(payload["base_url"]).strip():
            base = str(payload["base_url"]).strip().rstrip("/")
            self.buddy.llm.base_url = base
        if "api_key" in payload and str(payload["api_key"]).strip():
            self.buddy.llm.api_key = str(payload["api_key"]).strip()
        return {"ok": True, "llm_ready": self.llm_ready}

    def clear_memory(self) -> dict[str, Any]:
        memory = self.buddy.memory
        memory.facts.clear()
        memory.episodes.clear()
        memory.topics.clear()
        memory.save()
        return {"ok": True}

    def update_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        courses = payload.get("courses")
        preferences = payload.get("preferences")
        self.buddy.memory.set_profile(
            name=str(payload["name"]) if "name" in payload else None,
            major=str(payload["major"]) if "major" in payload else None,
            grade=str(payload["grade"]) if "grade" in payload else None,
            courses=[str(item) for item in courses] if isinstance(courses, list) else None,
            preferences=[str(item) for item in preferences] if isinstance(preferences, list) else None,
        )
        return {"ok": True, "profile": self.buddy.memory.profile.to_dict()}

    def quiz(self, payload: dict[str, Any]) -> dict[str, Any]:
        count = int(payload.get("count") or 5)
        with self._lock:
            quiz = self.buddy.quiz(
                book=str(payload["book"]) if payload.get("book") else None,
                topic=str(payload["topic"]) if payload.get("topic") else None,
                count=max(1, min(count, 10)),
            )
        return {
            "topic": quiz.topic,
            "book": quiz.book,
            "questions": [
                {
                    "question": question.question,
                    "answer": question.answer,
                    "hint": question.hint,
                    "difficulty": question.difficulty,
                }
                for question in quiz.questions
            ],
        }

    def summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        book = str(payload.get("book") or "").strip()
        if not book:
            raise StudyErrorBadRequest("请先选择一本教材")
        with self._lock:
            answer = self.buddy.summary(book=book, chapter=payload.get("chapter") or None)
        return {"text": answer.text, "sources": len(answer.sources)}

    def grade(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raise StudyErrorBadRequest("缺少话题")
        state = self.buddy.grade(topic, correct=bool(payload.get("correct")))
        return {"ok": True, "topic": state.name, "mastery": state.mastery, "due": state.due}

    def review(self, limit: int = 3) -> dict[str, Any]:
        with self._lock:
            items = self.buddy.review(limit=max(1, min(limit, 10)))
        return {
            "items": [
                {"topic": item.topic, "mastery": item.mastery, "question": item.question} for item in items
            ]
        }

    # -- routing -------------------------------------------------------------------

    def dispatch(
        self, method: str, path: str, payload: dict[str, Any] | None, query: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        payload = payload or {}
        try:
            if method == "GET" and path == "/api/state":
                return 200, self.state()
            if method == "GET" and path == "/api/memory":
                return 200, self.memory_payload()
            if method == "GET" and path == "/api/review":
                return 200, self.review(int(query.get("limit", ["3"])[0]))
            if method == "POST" and path == "/api/ask":
                return 200, self.ask(
                    str(payload.get("question") or ""),
                    rewrite=bool(payload.get("rewrite")),
                    semantic=bool(payload.get("semantic", True)),
                )
            if method == "POST" and path == "/api/add-book":
                return 200, self.add_book(payload)
            if method == "POST" and path == "/api/add-text":
                return 200, self.add_text(payload)
            if method == "POST" and path == "/api/config":
                return 200, self.configure(payload)
            if method == "POST" and path == "/api/memory/clear":
                return 200, self.clear_memory()
            if method == "POST" and path == "/api/profile":
                return 200, self.update_profile(payload)
            if method == "POST" and path == "/api/quiz":
                return 200, self.quiz(payload)
            if method == "POST" and path == "/api/summary":
                return 200, self.summary(payload)
            if method == "POST" and path == "/api/grade":
                return 200, self.grade(payload)
            return 404, {"error": f"unknown endpoint: {method} {path}"}
        except StudyErrorBadRequest as exc:
            return 400, {"error": str(exc)}
        except StudyError as exc:
            return 400, {"error": str(exc)}
        except LLMError as exc:
            return 502, {"error": f"模型调用失败: {exc}"}
        except Exception as exc:  # noqa: BLE001 - the UI needs a message, not a traceback
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


class _Handler(BaseHTTPRequestHandler):
    app: WebApp
    server_version = f"studybuddy/{__version__}"

    def log_message(self, *args: Any) -> None:  # keep the terminal quiet
        pass

    # -- responses -----------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- verbs ---------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            status, payload = self.app.dispatch("GET", parsed.path, None, parse_qs(parsed.query))
            self._send_json(status, payload)
            return
        if parsed.path in ("/", "/index.html"):
            self._send_file(_STATIC_DIR / "index.html")
            return
        self._send_file(_STATIC_DIR / parsed.path.lstrip("/"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "请求体必须是 JSON 对象"})
            return

        status, response = self.app.dispatch("POST", parsed.path, payload, parse_qs(parsed.query))
        self._send_json(status, response)


def serve(
    buddy: StudyBuddy,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the UI until interrupted."""
    app = WebApp(buddy)
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_address[1]}"

    print(f"studybuddy {__version__}  →  {url}", flush=True)
    print(f"数据目录: {buddy.library.root}", flush=True)
    if not app.llm_ready:
        print("提示: 还没有配置模型。在页面右上角「设置」里填 API Key（只存在内存里）。", flush=True)
    print("按 Ctrl-C 退出", flush=True)

    if open_browser:
        threading.Timer(0.6, lambda: __import__("webbrowser").open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")
    finally:
        server.server_close()
