"""A small local web UI, served by the standard library.

Design constraints, in order of importance:

1. **No new dependencies.** ``http.server`` plus a static HTML/CSS/JS bundle.
2. **Local by default.** Binds to 127.0.0.1, rejects non-loopback Host/Origin, and
   keeps the API key in process memory only.
3. **Thin.** It calls :class:`StudyBuddy` and returns JSON -- no business logic here.

Security posture (this is a localhost tool, but a browser can still be tricked into
talking to it):

* static paths are resolved and must stay inside ``webui/``
* uploaded filenames must be plain basenames -- no separators, no ``..``
* POST requires ``Content-Type: application/json`` and a bounded body, which also
  defeats cross-site "simple requests" that skip CORS preflight
* errors never echo tracebacks; user mistakes are 400, not 500
"""

from __future__ import annotations

import base64
import binascii
import difflib
import ipaddress
import json
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import config as saved_config
from .errors import AgentError, LLMError, StudyError
from .study import StudyBuddy

__all__ = ["WebApp", "serve"]

_STATIC_DIR = (Path(__file__).parent / "webui").resolve()
_MAX_UPLOAD_BYTES = 80 * 1024 * 1024
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_UPLOAD_BODY_BYTES = 4 * ((_MAX_UPLOAD_BYTES + 2) // 3) + 64 * 1024
_MAX_QUESTION_CHARS = 8000
_LOOPBACK_NAMES = {"localhost"}
_UI_CONFIG_FILE = "ui-config.json"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


class StudyErrorBadRequest(Exception):
    """Raised for bad user input; mapped to HTTP 400."""


def _host_allowed(value: str | None) -> bool:
    """True for loopback Host/Origin values (handles ``host:port`` and ``[::1]``)."""
    if not value:
        return False
    host = value.strip()
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8765
        host = host[1 : host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_allowed(origin: str) -> bool:
    if origin == "null":
        return False
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and _host_allowed(parsed.netloc)


_CHAPTER = re.compile(r"^第\s*([0-9]+)\s*[章节]")
_CN_CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百零〇]+\s*[章节]")
_NUMBERED = re.compile(r"^(\d{1,2})(?:\.\d{1,2})*\s")


def _plausible_heading(section: str) -> bool:
    """Reject OCR garbage such as "51.2 事件与集合" (a misread 1.2)."""
    if not 4 <= len(section) <= 30:
        return False
    match = _CHAPTER.match(section) or _NUMBERED.match(section)
    if match:
        return int(match.group(1)) <= 30
    return bool(_CN_CHAPTER.match(section))


def _int_param(value: Any, field: str, *, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StudyErrorBadRequest(f"{field} 必须是整数") from exc
    return max(low, min(parsed, high))


class WebApp:
    """The API surface the single page app talks to.

    ``serve`` enables background memory; direct callers retain synchronous writes
    by default. Call ``close`` to drain background tasks before disposing the app.
    """

    def __init__(self, buddy: StudyBuddy, *, background_memory: bool = False) -> None:
        self.buddy = buddy
        self._lock = threading.RLock()
        self._memory_executor = ThreadPoolExecutor(max_workers=1) if background_memory else None
        self._memory_slots = threading.BoundedSemaphore(8)
        self._memory_generation = 0
        self.restore_config()

    # -- persisted (non-secret) settings -------------------------------------------

    @property
    def config_path(self) -> Path:
        return saved_config.path_for(self.buddy.root)

    def restore_config(self) -> None:
        """Re-apply the model choice from the last run (the key is never stored)."""
        saved_config.apply_to(self.buddy)

    def save_config(self) -> None:
        """Persist the model choice; a read-only data directory must not break the UI."""
        if not saved_config.save(
            self.buddy.root,
            model=self.buddy.model,
            base_url=getattr(self.buddy.llm, "base_url", ""),
        ):
            print(
                f"[web] 无法保存设置到 {self.config_path}。本次仍然生效，但重启后会丢失。",
                file=sys.stderr,
                flush=True,
            )

    # -- helpers -------------------------------------------------------------------

    @property
    def llm_ready(self) -> bool:
        key = getattr(self.buddy.llm, "api_key", "")
        return bool(key) and key != "not-needed"

    @property
    def web_provider(self) -> str:
        client = self.buddy.ensure_search()
        return getattr(client, "name", "") if client is not None else ""

    @property
    def web_ready(self) -> bool:
        return self.buddy.ensure_search() is not None

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
            "suggestions": self.suggestions(),
            "writable": self.library_writable(),
            "web_ready": self.web_ready,
            "web_provider": self.web_provider,
        }

    def library_writable(self) -> bool:
        """Can we write to the data directory? Read-only volumes are common."""
        probe = Path(self.buddy.root) / ".write-probe"
        try:
            probe.write_text("ok", "utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def suggestions(self) -> list[str]:
        """Three questions derived from the book's own section titles.

        OCR gives near-duplicates ("测度" vs "測度") and occasional impossible
        chapter numbers ("51.2" for 1.2), so candidates are filtered by shape and
        de-duplicated by string similarity.
        """
        picks: list[str] = []
        for chunk in self.buddy.library.chunks:
            if chunk.nav or not chunk.section:
                continue
            section = chunk.section.strip()
            if not _plausible_heading(section):
                continue
            if any(difflib.SequenceMatcher(None, section, kept).ratio() > 0.85 for kept in picks):
                continue
            picks.append(section)
            if len(picks) >= 3:
                break
        return [f"{section} 讲了什么？" for section in picks]

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

    def ask(
        self,
        question: str,
        *,
        rewrite: bool = False,
        semantic: bool = True,
        web: bool = False,
        remember: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        question = (question or "").strip()
        if not question:
            raise StudyErrorBadRequest("问题是空的")
        if len(question) > _MAX_QUESTION_CHARS:
            raise StudyErrorBadRequest(f"问题太长（上限 {_MAX_QUESTION_CHARS} 字）")
        if not self.buddy.library.chunks:
            raise StudyError("书库是空的 -- 先在「书库」里导入一本教材")
        # Only a real HTTP client needs a key; injected clients (tests, custom
        # providers) manage their own credentials.
        from .llm import LLM

        if isinstance(self.buddy.llm, LLM) and not self.llm_ready:
            raise StudyErrorBadRequest("还没有配置模型：点右上角「设置」粘贴 API Key 后再提问")

        with self._lock:
            answer = self.buddy.ask(
                question,
                rewrite=rewrite,
                semantic=semantic,
                web=web,
                remember=remember,
                persist_memory=self._memory_executor is None,
            )
            if remember and self._memory_executor is not None:
                if self._memory_slots.acquire(blocking=False):
                    snapshot = copy(self.buddy)
                    snapshot.llm = copy(self.buddy.llm)
                    generation = self._memory_generation

                    def update_memory() -> None:
                        try:
                            books = list(dict.fromkeys(s.chunk.book_title for s in answer.sources))[:3]
                            topics, facts, summary = snapshot._extract_memory(question, answer.text, books)
                            with self._lock:
                                if generation == self._memory_generation:
                                    snapshot._record_memory(question, answer, books, topics, facts, summary)
                        except Exception:
                            print("[web] 后台记忆提取失败", file=sys.stderr, flush=True)
                        finally:
                            self._memory_slots.release()

                    self._memory_executor.submit(update_memory)
                else:
                    answer.warnings.append("记忆任务繁忙，本次未保存学习记忆")

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
            "web_sources": [
                {"index": item.index, "title": item.title, "url": item.url} for item in answer.web_sources
            ],
            "warnings": answer.warnings,
            "elapsed": round(time.perf_counter() - started, 2),
            "usage": {
                "prompt_tokens": answer.usage.prompt_tokens,
                "completion_tokens": answer.usage.completion_tokens,
                "total_tokens": answer.usage.total_tokens,
            },
        }

    def add_book(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_name = str(payload.get("name") or "教材.pdf")
        # The name reaches the filesystem: accept a basename and nothing else.
        if raw_name in {".", ".."} or "/" in raw_name or "\\" in raw_name or "\x00" in raw_name:
            raise StudyErrorBadRequest("文件名不合法（不允许包含路径分隔符）")
        name = Path(raw_name).name

        encoded = payload.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise StudyErrorBadRequest("没有收到文件内容")

        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StudyErrorBadRequest(f"文件内容不是有效的 base64: {exc}") from exc
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise StudyErrorBadRequest("文件太大（上限 80MB）")

        upload_dir = (Path(self.buddy.root) / "uploads").resolve()
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StudyError(f"数据目录不可写，无法保存上传的文件：{exc}") from exc
        temporary = (upload_dir / name).resolve()
        if not temporary.is_relative_to(upload_dir):
            raise StudyErrorBadRequest("文件名不合法（不允许包含路径分隔符）")
        try:
            temporary.write_bytes(raw)
        except OSError as exc:
            raise StudyError(f"数据目录不可写，无法保存上传的文件：{exc}") from exc

        try:
            with self._lock:
                book = self.buddy.add_book(
                    temporary,
                    course=str(payload.get("course") or ""),
                    title=str(payload.get("title") or "") or None,
                )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
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
        for field in ("model", "base_url", "api_key"):
            if field in payload and not isinstance(payload[field], str):
                raise StudyErrorBadRequest(f"{field} 必须是字符串")

        if str(payload.get("model", "")).strip():
            self.buddy.model = str(payload["model"]).strip()
            self.buddy.llm.model = self.buddy.model
        if str(payload.get("base_url", "")).strip():
            base = str(payload["base_url"]).strip().rstrip("/")
            self.buddy.llm.base_url = base
        if str(payload.get("api_key", "")).strip():
            self.buddy.llm.api_key = str(payload["api_key"]).strip()
        self.save_config()
        return {"ok": True, "llm_ready": self.llm_ready, "model": self.buddy.model}

    def close(self) -> None:
        """Finish queued memory work before closing the application."""
        if self._memory_executor is not None:
            self._memory_executor.shutdown(wait=True)

    def clear_memory(self) -> dict[str, Any]:
        with self._lock:
            self._memory_generation += 1
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
        count = _int_param(payload.get("count", 5), "count", low=1, high=10)
        with self._lock:
            quiz = self.buddy.quiz(
                book=str(payload["book"]) if payload.get("book") else None,
                topic=str(payload["topic"]) if payload.get("topic") else None,
                count=count,
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

    def review(self, limit: Any = 3) -> dict[str, Any]:
        count = _int_param(limit, "limit", low=1, high=10)
        with self._lock:
            items = self.buddy.review(limit=count)
        return {
            "items": [
                {"topic": item.topic, "mastery": item.mastery, "question": item.question} for item in items
            ]
        }

    # -- routing -------------------------------------------------------------------

    def dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        query: dict[str, list[str]],
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            return self._dispatch(method, path, payload, query)

    def _dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        query: dict[str, list[str]],
    ) -> tuple[int, dict[str, Any]]:
        payload = payload or {}
        try:
            if method == "GET" and path == "/api/state":
                return 200, self.state()
            if method == "GET" and path == "/api/memory":
                return 200, self.memory_payload()
            if method == "GET" and path == "/api/review":
                return 200, self.review(query.get("limit", ["3"])[0])
            if method == "POST" and path == "/api/ask":
                return 200, self.ask(
                    str(payload.get("question") or ""),
                    rewrite=bool(payload.get("rewrite")),
                    semantic=bool(payload.get("semantic", True)),
                    web=bool(payload.get("web")),
                    remember=bool(payload.get("remember", True)),
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
        except LLMError as exc:
            return 502, {"error": f"模型调用失败: {exc}"}
        except AgentError as exc:
            # PDFError / LibraryError / StudyError: the user's input or data is at fault.
            return 400, {"error": str(exc)}
        except Exception:  # noqa: BLE001 - never leak internals to the client
            # The client gets a generic message, so the detail must go somewhere:
            # the terminal that is running `studybuddy ui`.
            print(f"[web] 内部错误 {method} {path}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            return 500, {"error": "服务器内部错误，详情见终端日志"}


class _Handler(BaseHTTPRequestHandler):
    app: WebApp
    server_version = f"studybuddy/{__version__}"
    timeout = 20  # a stalled client must not hold a thread forever

    def log_message(self, *args: Any) -> None:  # keep the terminal quiet
        pass

    # -- responses -----------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        # Revalidate every time: a stale app.js would keep showing raw LaTeX after
        # an upgrade.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        return _host_allowed(self.headers.get("Host"))

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:  # curl and other non-browser clients
            return True
        return _origin_allowed(origin)

    # -- verbs ---------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_ok():
            self._send_json(403, {"error": "host not allowed"})
            return

        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            status, payload = self.app.dispatch("GET", parsed.path, None, parse_qs(parsed.query))
            self._send_json(status, payload)
            return

        relative = parsed.path.lstrip("/") or "index.html"
        try:
            candidate = (_STATIC_DIR / relative).resolve()
            candidate.relative_to(_STATIC_DIR)  # containment: no escaping webui/
        except (ValueError, OSError):
            self._send_json(404, {"error": "not found"})
            return
        self._send_file(candidate)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_ok():
            self._send_json(403, {"error": "host not allowed"})
            return
        if not self._origin_ok():
            self._send_json(403, {"error": "origin not allowed"})
            return

        # Requiring a JSON content type blocks cross-site "simple requests", which
        # skip CORS preflight and could otherwise drive this API.
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "Content-Type 必须是 application/json"})
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json(411, {"error": "缺少 Content-Length"})
            return
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "Content-Length 非法"})
            return
        if length < 0:
            self._send_json(400, {"error": "Content-Length 非法"})
            return
        limit = _MAX_UPLOAD_BODY_BYTES if urlparse(self.path).path == "/api/add-book" else _MAX_BODY_BYTES
        if length > limit:
            self._send_json(413, {"error": "请求体过大"})
            return

        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "请求体必须是 JSON 对象"})
            return

        status, response = self.app.dispatch("POST", urlparse(self.path).path, payload, {})
        self._send_json(status, response)

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"error": "method not allowed"})

    do_PUT = _method_not_allowed  # noqa: N815
    do_DELETE = _method_not_allowed  # noqa: N815
    do_PATCH = _method_not_allowed  # noqa: N815
    do_OPTIONS = _method_not_allowed  # noqa: N815


def serve(
    buddy: StudyBuddy,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the UI until interrupted."""
    app = WebApp(buddy, background_memory=True)
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_address[1]}"

    print(f"studybuddy {__version__}  →  {url}", flush=True)
    print(f"数据目录: {buddy.library.root}", flush=True)

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"警告: 正在监听 {host}，局域网内其他设备可以访问这个界面。"
            "它没有登录机制，请只在可信网络里这样做。",
            file=sys.stderr,
            flush=True,
        )
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
        app.close()
