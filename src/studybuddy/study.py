"""The study assistant: retrieve, answer with citations, remember, schedule review.

The pipeline for a question is intentionally explicit rather than agentic:

1. retrieve the most relevant passages across the student's books
2. assemble a prompt from *profile + recalled memory + passages*
3. answer with ``[n]`` citations back to real pages
4. extract durable memories and record the episode

Determinism matters more than autonomy here: a student must be able to trust that an
answer came from page 143 of the book they uploaded, not from the model's imagination.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .embed import DEFAULT_RRF_K, Embedder
from .errors import StudyError
from .llm import LLM
from .local_embed import DEFAULT_LOCAL_MODEL, LocalEmbedder
from .memory import Memory
from .search import SearchError, WebSearch, build_search
from .store import Library
from .types import Answer, Book, Chunk, Message, Source, WebSource

__all__ = ["StudyBuddy", "QuizQuestion", "Quiz", "Comparison", "ReviewItem"]

_CITATION = re.compile(r"\[(W?)(\d+)\]")
# "条件概率和事件的独立性" asks about two things; BM25 would otherwise reward the
# document that matches whichever half has more words, and drop the other half.
_COMPOUND = re.compile(r"\s*(?:以及|和|与|、)\s*")


_QUESTION_TAIL = re.compile(
    r"(之间)?(?:有什么(?:关系|区别|不同|联系|用)?|是什么|有哪些(?:形式|区别)?|如何|怎么(?:样)?|为什么|怎么理解)$"
)


def split_query(text: str) -> list[str]:
    """Split a compound Chinese question into its topical parts (best effort).

    Only *topics* are kept: interrogative tails are stripped and fragments shorter
    than four characters are dropped, so "中心极限定理的条件和结论是什么" does not
    degenerate into a useless "结论是什么" query.
    """
    if len(text.strip()) < 8:
        return []
    parts: list[str] = []
    for raw in _COMPOUND.split(text):
        part = raw.strip(" ？?。，,；;：:")
        part = _QUESTION_TAIL.sub("", part).strip(" 的")
        if len(part) >= 4:
            parts.append(part)
    return parts if len(parts) >= 2 else []


SYSTEM_PROMPT = """你是一位严谨、耐心的大学学业辅导老师，正在辅导一名大学生理解他自己上传的教材。

必须遵守：
1. 只依据【教材片段】作答。片段中没有的内容，明确说明“教材中未找到”，绝不编造。
2. 引用片段时，在句子末尾用 [编号] 标注，编号就是片段前的数字，可以同时引用多个如 [1][3]。
3. 如果学生画像或记忆显示这是他的薄弱点，多给一步推导和一个具体例子。
4. 中文作答，结论先行再给推导，避免空洞的鼓励和重复原话。
5. 数学符号用普通字符书写（如 f^{-1}、R^n、sigma、sqrt），不要使用 𝒬、𝓕、ℝ 这类
   装饰性 Unicode 数学字母——它们在很多字体里会显示成方框。
6. 如果提供了【网络搜索结果】：优先使用教材片段；教材没有覆盖时才引用网络内容，
   引用时用 [W1]、[W2] 标注，并在句子里说明“这是网络资料”。网络内容与教材冲突时，
   明确说明两者不一致，不要默默选一个。"""

QUIZ_SYSTEM = """你是一位大学课程的命题老师。你只输出 JSON，不输出任何解释文字。"""

SUMMARY_SYSTEM = """你是一位擅长做知识梳理的大学助教。你的提纲紧凑、可执行，学生照着就能复习。"""

COMPARE_SYSTEM = """你是一位擅长辨析相近概念的大学老师。你总能一眼看出学生混淆它们的原因。"""

MEMORY_SYSTEM = """你在维护一名学生的学习记忆库。你只输出 JSON，不输出任何解释文字。"""

REWRITE_SYSTEM = """你是教材检索助手。你只输出 JSON 数组，不输出任何解释文字。"""


_EXOTIC_MATH = re.compile(r"[\U0001D400-\U0001D7FF]")


def normalize_math_text(text: str) -> str:
    """Map decorative Unicode math letters (𝒬, 𝓕, ℝ) back to plain ASCII.

    Those code points live in a supplementary plane that many UI fonts do not
    cover, so they render as tofu boxes -- which users read as "garbled text".
    NFKC maps them to their ASCII base letters.
    """
    if not text:
        return text
    return _EXOTIC_MATH.sub(lambda match: unicodedata.normalize("NFKC", match.group()), text)


def _loads_json_array(text: str) -> list[str] | None:
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [str(item).strip() for item in payload if str(item).strip()]


@dataclass(slots=True)
class QuizQuestion:
    question: str
    answer: str = ""
    hint: str = ""
    difficulty: str = "medium"


@dataclass(slots=True)
class Quiz:
    topic: str = ""
    book: str = ""
    questions: list[QuizQuestion] = field(default_factory=list)

    def render(self, *, show_answers: bool = False) -> str:
        lines: list[str] = []
        if self.topic:
            lines.append(f"主题: {self.topic}" + (f"（{self.book}）" if self.book else ""))
            lines.append("")
        for number, question in enumerate(self.questions, start=1):
            lines.append(f"{number}. {question.question}")
            if show_answers:
                if question.hint:
                    lines.append(f"   提示: {question.hint}")
                lines.append(f"   答案: {question.answer}")
                lines.append(f"   难度: {question.difficulty}")
            lines.append("")
        return "\n".join(lines).rstrip()


@dataclass(slots=True)
class Comparison:
    text: str
    sources: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def with_citations(self) -> str:
        if not self.sources:
            return self.text
        lines = [self.text, "", "来源:"]
        lines.extend(f"  [{source.index}] {source.citation}" for source in self.sources)
        return "\n".join(lines)


@dataclass(slots=True)
class ReviewItem:
    topic: str
    mastery: float
    question: str


def _loads_json_block(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply, tolerating markdown fences."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class StudyBuddy:
    """The facade a CLI, a web app or a notebook talks to."""

    def __init__(
        self,
        root: str | Path = ".studybuddy",
        *,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.2,
        llm: Any = None,
        library: Library | None = None,
        memory: Memory | None = None,
        embedding_model: str | None = None,
        embedding_base_url: str | None = None,
        embedding_provider: str | None = None,
        embedder: Any = None,
        search_client: WebSearch | None = None,
        search_provider: str | None = None,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        self.root = Path(root)
        self.model = model
        self.library = library or Library(self.root)
        self.memory = memory or Memory(self.root)
        self.llm = llm or LLM(model=model, api_key=api_key, base_url=base_url, temperature=temperature)
        self.session: list[Message] = []

        self._api_key = api_key
        self._base_url = base_url
        self.embedding_model = embedding_model or os.environ.get("STUDYBUDDY_EMBED_MODEL") or None
        self.embedding_base_url = embedding_base_url or os.environ.get("STUDYBUDDY_EMBED_BASE_URL") or None
        self.embedding_provider = embedding_provider or os.environ.get("STUDYBUDDY_EMBED_PROVIDER") or None
        self.search_client = search_client
        self.search_provider = search_provider or os.environ.get("STUDYBUDDY_SEARCH_PROVIDER") or None
        self.search_error: str | None = None
        self._search_checked = False
        self.embedder: Any = embedder
        self.rrf_k = rrf_k
        self.embedding_error: str | None = None

    # -- ingest --------------------------------------------------------------------

    def add_document(
        self,
        *,
        title: str,
        pages: Iterable[tuple[int, str]],
        course: str = "",
        source: str = "",
        embed: bool = True,
        on_progress: Any = None,
    ) -> Book:
        """Index text that was extracted elsewhere (OCR, EPUB, pasted notes)."""
        book = self.library.add_document(
            title=title, pages=pages, course=course, source=source, on_progress=on_progress
        )
        if embed and self.ensure_embedder() is not None:
            try:
                self.library.build_embeddings(self.embedder)
            except Exception as exc:  # noqa: BLE001 - still lexically searchable
                self.embedding_error = f"{type(exc).__name__}: {exc}"
        if course:
            courses = list(dict.fromkeys([*self.memory.profile.courses, course]))
            self.memory.set_profile(courses=courses)
        return book

    def add_book(
        self,
        path: str | Path,
        *,
        course: str = "",
        title: str | None = None,
        on_progress: Any = None,
        embed: bool = True,
    ) -> Book:
        book = self.library.add_book(path, course=course, title=title, on_progress=on_progress)
        if embed and self.ensure_embedder() is not None:
            try:
                self.library.build_embeddings(self.embedder)
            except Exception as exc:  # noqa: BLE001 - the book is still lexically searchable
                self.embedding_error = f"{type(exc).__name__}: {exc}"
        if course:
            courses = list(dict.fromkeys([*self.memory.profile.courses, course]))
            self.memory.set_profile(courses=courses)
        return book

    # -- ask -----------------------------------------------------------------------

    def ask(
        self,
        question: str,
        *,
        books: list[str] | None = None,
        top_k: int = 6,
        remember: bool = True,
        persist_memory: bool = True,
        per_book_cap: int = 3,
        semantic: bool = True,
        rewrite: bool = False,
        web: bool = False,
    ) -> Answer:
        """Answer with citations checked against retrieved source numbers.

        ``remember=False`` disables memory reads and writes. ``persist_memory=False``
        keeps personalized context but lets the web layer schedule memory writes.
        Number validation does not establish that a claim follows from its source.
        """
        question = question.strip()
        if not question:
            raise StudyError("问题是空的")
        if not self.library.chunks:
            raise StudyError("书库是空的 -- 先用 `studybuddy add` 导入一本教材")

        warnings: list[str] = []
        web_sources = self._web_results(question, warnings) if web else []

        selected = self.library.resolve_books(books)
        hits, warning = self._retrieve(
            question,
            books=selected,
            top_k=top_k,
            per_book_cap=per_book_cap,
            semantic=semantic,
            rewrite=rewrite,
        )
        sources = [
            Source(index=index + 1, chunk=chunk, score=score) for index, (chunk, score) in enumerate(hits)
        ]

        if warning:
            warnings.append(warning)

        messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user", content=self._ask_prompt(question, sources, web_sources, include_memory=remember)
            ),
        ]
        response = self.llm.complete(messages)

        answer = Answer(
            text=normalize_math_text((response.text or "").strip()),
            sources=sources,
            memories=[hit.text for hit in self.memory.recall(question, limit=4)] if remember else [],
            web_sources=web_sources,
            warnings=warnings,
            usage=response.usage,
            model=response.model or self.model,
        )

        valid_citations = {str(source.index) for source in sources}
        valid_citations.update(f"W{source.index}" for source in web_sources)
        invalid: set[str] = set()
        cited: set[str] = set()

        def check_citation(match: re.Match[str]) -> str:
            label = match.group(1) + match.group(2)
            if label not in valid_citations:
                invalid.add(label)
                return "【无效引用】"
            cited.add(label)
            return match.group(0)

        answer.text = _CITATION.sub(check_citation, answer.text)
        if invalid:
            answer.warnings.append("已移除不存在的来源编号：" + "、".join(sorted(invalid)))
        if valid_citations and not cited:
            answer.warnings.append("回答未包含有效来源引用，请核对教材原文")
        if not valid_citations:
            answer.warnings.append("未检索到可核对的来源，请勿将本回答视为教材结论")

        self.session.append(Message(role="user", content=question))
        self.session.append(Message(role="assistant", content=answer.text))

        if remember and persist_memory:
            self.remember(question, answer)
        return answer

    def _web_results(self, question: str, warnings: list[str]) -> list[WebSource]:
        """Search the web, degrading to a warning when the provider fails."""
        client = self.ensure_search()
        if client is None:
            warnings.append(self.search_error or "未配置联网搜索服务")
            return []
        try:
            results = client.search(question, limit=5)
        except Exception as exc:  # noqa: BLE001 - search must never block an answer
            warnings.append(f"联网搜索失败：{exc}")
            return []
        return [
            WebSource(index=index, title=item.title, url=item.url, snippet=item.snippet)
            for index, item in enumerate(results, start=1)
        ]

    def ensure_search(self) -> WebSearch | None:
        if self.search_client is not None:
            return self.search_client
        if self._search_checked:
            return None
        self._search_checked = True
        try:
            self.search_client = build_search(self.search_provider)
        except SearchError as exc:
            self.search_error = str(exc)
            return None
        return self.search_client

    def _ask_prompt(
        self,
        question: str,
        sources: list[Source],
        web_sources: list[WebSource] | None = None,
        *,
        include_memory: bool = True,
    ) -> str:
        blocks: list[str] = []

        memory_block = self.memory.context_block(question) if include_memory else ""
        blocks.append("【学生情况】")
        blocks.append(memory_block or "（还没有画像和记忆）")
        blocks.append("")

        if self.session:
            blocks.append("【本次会话最近对话】")
            for message in self.session[-4:]:
                who = "学生" if message.role == "user" else "你"
                blocks.append(f"{who}: {(message.content or '')[:300]}")
            blocks.append("")

        blocks.append("【教材片段】")
        if not sources:
            blocks.append("（没有检索到相关片段）")
        for source in sources:
            blocks.append(f"[{source.index}] {source.citation}")
            blocks.append(source.chunk.text.strip()[:1600])
            if source.chunk.unreliable:
                blocks.append(
                    "（提示：该片段公式较多，OCR 可能失真；引用公式、条件或数值前，"
                    "请说明不确定并要求读者核对原书。）"
                )
            blocks.append("")

        if web_sources:
            blocks.append("【网络搜索结果】（仅当教材没有答案时使用，引用用 [W编号]）")
            for item in web_sources:
                blocks.append(f"[W{item.index}] {item.title} — {item.url}")
                if item.snippet:
                    blocks.append(item.snippet[:300])
                blocks.append("")

        blocks.append("【问题】")
        blocks.append(question)
        blocks.append("")
        blocks.append("如果片段不足以回答，请直接说明缺什么；引用时用 [编号]。")
        return "\n".join(blocks)

    def _retrieve(
        self,
        question: str,
        *,
        books: list[Book],
        top_k: int,
        per_book_cap: int,
        semantic: bool,
        rewrite: bool = False,
    ) -> tuple[list[tuple[Chunk, float]], str | None]:
        """Search, then make sure a compound question covers all of its parts."""
        warning: str | None = None

        def search(text: str, limit: int) -> list[tuple[Chunk, float]]:
            nonlocal warning
            vector, warn = self._query_vector(text) if semantic else (None, None)
            warning = warning or warn
            return self.library.search(
                text,
                books=books,
                top_k=limit,
                per_book_cap=per_book_cap,
                query_vector=vector,
                rrf_k=self.rrf_k,
            )

        primary = search(question, top_k)
        head = primary[:2]
        seen = {chunk.id for chunk, _score in head}
        coverage: list[tuple[Chunk, float]] = []

        # Section titles are the book's own vocabulary: pull the opening chunk of
        # the best-matching sections so a definition buried at the start of a
        # chapter is not lost behind its exercises.
        for lead in self.library.section_leads(question, books=books, limit=2):
            if lead.id in seen:
                continue
            seen.add(lead.id)
            coverage.append((lead, 0.0))

        for query in self.rewrite_queries(question) if rewrite else []:
            for chunk, score in search(query, max(2, top_k // 3)):
                if chunk.id in seen:
                    continue
                seen.add(chunk.id)
                coverage.append((chunk, score))

        for part in split_query(question):
            for chunk, score in search(part, max(2, top_k // 3)):
                if chunk.id in seen:
                    continue
                seen.add(chunk.id)
                coverage.append((chunk, score))
        tail = [(chunk, score) for chunk, score in primary[2:] if chunk.id not in seen]
        return (head + coverage + tail)[:top_k], warning

    # -- memory --------------------------------------------------------------------

    def remember(self, question: str, answer: Answer) -> None:
        """Persist the episode and extract durable facts about the student."""
        books = list(dict.fromkeys(source.chunk.book_title for source in answer.sources))[:3]
        topics, facts, summary = self._extract_memory(question, answer.text, books)
        self._record_memory(question, answer, books, topics, facts, summary)

    def _record_memory(
        self,
        question: str,
        answer: Answer,
        books: list[str],
        topics: list[str],
        facts: list[dict[str, str]],
        summary: str,
    ) -> None:
        """Apply extracted memory separately so web workers only lock the write."""
        try:
            self.memory.record_episode(
                question,
                summary=summary or answer.text[:200],
                topics=topics,
                books=books,
            )
            for topic in topics:
                self.memory.note_topic(topic)
            for fact in facts:
                self.memory.add_fact(
                    fact["text"],
                    topic=fact.get("topic", ""),
                    kind=fact.get("kind", "fact"),
                )
        except OSError as exc:  # noqa: BLE001 - the answer is already produced
            answer.warnings.append(f"记忆没能保存（数据目录不可写：{exc}）")

    def _extract_memory(
        self, question: str, answer: str, books: list[str]
    ) -> tuple[list[str], list[dict[str, str]], str]:
        prompt = f"""基于下面这一轮问答，抽取值得长期记住的信息。

学生问: {question}
辅导回答: {answer[:1200]}
用到的教材: {", ".join(books) or "无"}

只输出 JSON：
{{"topics": ["知识点术语", "..."],
 "facts": [{{"text": "关于学生本人的、以后还有用的信息",
            "kind": "weakness|preference|goal|fact", "topic": "相关知识点"}}],
 "summary": "一句话概括这次问答"}}

要求：
- topics 用教材术语，1-3 个；不确定就给空数组
- facts 只记录学生的薄弱点、偏好、目标；没有就给空数组，不要记录一次性的闲聊
- 全部用中文"""

        try:
            response = self.llm.complete(
                [Message(role="system", content=MEMORY_SYSTEM), Message(role="user", content=prompt)]
            )
        except Exception:  # noqa: BLE001 - memory must never break an answer
            return [], [], ""

        payload = _loads_json_block(response.text or "")
        if not payload:
            return [], [], ""

        topics = [str(item).strip() for item in payload.get("topics") or [] if str(item).strip()][:3]
        raw_facts = payload.get("facts") or []
        facts: list[dict[str, str]] = []
        if isinstance(raw_facts, list):
            for item in raw_facts[:5]:
                if isinstance(item, dict) and str(item.get("text", "")).strip():
                    facts.append(
                        {
                            "text": str(item["text"]).strip(),
                            "kind": str(item.get("kind", "fact")).strip() or "fact",
                            "topic": str(item.get("topic", "")).strip(),
                        }
                    )
        summary = str(payload.get("summary") or "").strip()
        return topics, facts, summary

    # -- quiz ----------------------------------------------------------------------

    def quiz(
        self,
        *,
        book: str | None = None,
        topic: str | None = None,
        count: int = 5,
        difficulty: str = "medium",
    ) -> Quiz:
        """Generate comprehension questions from a book, a topic, or both."""
        chunks = self._material(book=book, topic=topic, limit=10)
        if not chunks:
            raise StudyError("没有找到可用于出题的内容，换一个主题或先导入教材")

        book_title = chunks[0].book_title
        focus = topic or (chunks[0].section if chunks else book_title)
        body = "\n\n".join(
            f"[{index}] {chunk.citation}\n{chunk.text[:1200]}" for index, chunk in enumerate(chunks, 1)
        )

        prompt = f"""根据下面的教材内容，出 {count} 道考查**理解**而不是死记硬背的题。
难度: {difficulty}
主题: {focus}

{body}

只输出 JSON：
{{"topic": "{focus}",
 "questions": [{{"question": "...", "answer": "...", "hint": "...",
                "difficulty": "easy|medium|hard"}}]}}"""

        response = self.llm.complete(
            [Message(role="system", content=QUIZ_SYSTEM), Message(role="user", content=prompt)]
        )
        payload = _loads_json_block(response.text or "") or {}

        questions = [
            QuizQuestion(
                question=normalize_math_text(str(item.get("question", "")).strip()),
                answer=normalize_math_text(str(item.get("answer", "")).strip()),
                hint=normalize_math_text(str(item.get("hint", "")).strip()),
                difficulty=str(item.get("difficulty", difficulty)).strip(),
            )
            for item in payload.get("questions") or []
            if isinstance(item, dict) and str(item.get("question", "")).strip()
        ]

        quiz = Quiz(topic=str(payload.get("topic") or focus), book=book_title, questions=questions[:count])
        if quiz.topic:
            self.memory.note_topic(quiz.topic)
        return quiz

    # -- summary -------------------------------------------------------------------

    def summary(self, *, book: str, chapter: str | None = None, limit: int = 12) -> Answer:
        """A revision outline for a book or one chapter of it."""
        chunks = self._material(book=book, topic=chapter, limit=limit, section_filter=chapter)
        if not chunks:
            raise StudyError(f"没有找到 {book!r} 的内容" + (f"（{chapter!r} 章）" if chapter else ""))

        sources = [Source(index=index + 1, chunk=chunk) for index, chunk in enumerate(chunks)]
        body = "\n\n".join(
            f"[{source.index}] {source.citation}\n{source.chunk.text[:1200]}" for source in sources
        )

        prompt = f"""请为下面的教材内容写一份复习提纲，帮助学生在一周内掌握它。

{body}

输出 markdown，结构固定：
## 一句话概括
## 核心概念（每个概念一句话 + 它在考试里通常怎么考）
## 关键公式 / 结论
## 易错点（至少 3 条）
## 自测题（3 道，只给题目不给答案）

引用教材时用 [编号]。"""

        response = self.llm.complete(
            [Message(role="system", content=SUMMARY_SYSTEM), Message(role="user", content=prompt)]
        )
        return Answer(
            text=normalize_math_text((response.text or "").strip()),
            sources=sources,
            usage=response.usage,
            model=response.model or self.model,
        )

    # -- compare -------------------------------------------------------------------

    def compare(self, concept_a: str, concept_b: str, *, top_k: int = 4) -> Comparison:
        """Explain the difference between two concepts using passages for each."""
        vector_a, warning_a = self._query_vector(concept_a)
        vector_b, warning_b = self._query_vector(concept_b)
        hits_a = self.library.search(concept_a, top_k=top_k, query_vector=vector_a, rrf_k=self.rrf_k)
        hits_b = self.library.search(concept_b, top_k=top_k, query_vector=vector_b, rrf_k=self.rrf_k)

        seen: set[str] = set()
        numbered: list[tuple[Chunk, float]] = []
        for chunk, score in [*hits_a, *hits_b]:
            if chunk.id in seen:
                continue
            seen.add(chunk.id)
            numbered.append((chunk, score))
        if not numbered:
            raise StudyError("这两个概念在你上传的教材里都没有出现")

        sources = [
            Source(index=index + 1, chunk=chunk, score=score) for index, (chunk, score) in enumerate(numbered)
        ]
        body = "\n\n".join(
            f"[{source.index}] {source.citation}\n{source.chunk.text[:1200]}" for source in sources
        )

        prompt = f"""学生在区分这两个概念：「{concept_a}」和「{concept_b}」。

{body}

请输出：
1. 一句话结论：它们的根本区别
2. 对比表格（定义 / 成立条件 / 结论差异 / 典型例题场景）
3. 学生最容易混淆的一点，以及一个记住它们的口诀或判据

引用教材用 [编号]。"""

        response = self.llm.complete(
            [Message(role="system", content=COMPARE_SYSTEM), Message(role="user", content=prompt)]
        )
        return Comparison(
            text=normalize_math_text((response.text or "").strip()),
            sources=sources,
            warnings=[w for w in (warning_a, warning_b) if w],
        )

    # -- review (spaced repetition) -------------------------------------------------

    def review(self, *, limit: int = 3) -> list[ReviewItem]:
        """Return due topics with one fresh question each."""
        due = self.memory.due_topics(limit=limit)
        items: list[ReviewItem] = []
        for topic in due:
            try:
                quiz = self.quiz(topic=topic.name, count=1)
            except StudyError:
                continue
            if not quiz.questions:
                continue
            items.append(
                ReviewItem(
                    topic=topic.name,
                    mastery=topic.mastery,
                    question=quiz.questions[0].question,
                )
            )
        return items

    def grade(self, topic: str, *, correct: bool) -> Any:
        """Record a self-assessed quiz result and reschedule the topic."""
        return self.memory.grade_topic(topic, correct=correct)

    # -- query rewriting -------------------------------------------------------------

    def rewrite_queries(self, question: str, *, limit: int = 3) -> list[str]:
        """Ask the model for 2-3 textbook-style search queries.

        Students write "为什么求导和微分是一回事"; textbooks write "导数与微分的关系".
        One cheap call bridges that gap for difficult questions. Returns an empty
        list on any failure -- retrieval then proceeds unchanged.
        """
        prompt = (
            "把学生的问题改写成 2-3 个适合在教材里做关键词检索的查询。\n"
            "要求：保留学科术语，去掉口语和疑问；每个查询 3-12 个字；不要重复原句。\n"
            '只输出 JSON 数组，例如 ["条件概率 定义", "独立性 判定"]。\n\n'
            f"问题：{question}"
        )
        try:
            response = self.llm.complete(
                [Message(role="system", content=REWRITE_SYSTEM), Message(role="user", content=prompt)]
            )
        except Exception:  # noqa: BLE001 - rewriting is an optimisation, not a requirement
            return []

        queries = _loads_json_array(response.text or "") or []
        cleaned: list[str] = []
        for query in queries:
            if query == question or query in cleaned:
                continue
            if 2 <= len(query) <= 40:
                cleaned.append(query)
        return cleaned[:limit]

    # -- embeddings ----------------------------------------------------------------

    def ensure_embedder(self) -> Any:
        """Return the embedding client, creating one from config when possible.

        ``local`` uses the optional fastembed extra and needs no API key; ``api``
        uses an OpenAI-compatible ``/embeddings`` endpoint. When the index already
        exists, the provider it was built with wins, so an existing library keeps
        working without reconfiguration.
        """
        if self.embedder is not None:
            return self.embedder

        model = self.embedding_model or self.library.embeddings_model
        provider = self.embedding_provider or self.library.embeddings_provider or "api"

        if provider == "local":
            self.embedder = LocalEmbedder(model or DEFAULT_LOCAL_MODEL)
            return self.embedder

        if not model:
            return None
        self.embedder = Embedder(
            model,
            api_key=self._api_key,
            base_url=self.embedding_base_url or self._base_url,
        )
        return self.embedder

    def build_index(self, *, force: bool = False, on_progress: Any = None) -> int:
        """Embed every chunk that does not have a vector yet."""
        embedder = self.ensure_embedder()
        if embedder is None:
            raise StudyError(
                "还没有配置 embedding 模型。请设置 STUDYBUDDY_EMBED_MODEL 和 "
                "STUDYBUDDY_EMBED_BASE_URL（或 --embed-model / --embed-base-url）"
            )
        if not self.library.chunks:
            raise StudyError("书库是空的，先导入教材再建索引")
        return self.library.build_embeddings(embedder, force=force, on_progress=on_progress)

    def _query_vector(self, text: str) -> tuple[list[float] | None, str | None]:
        """Embed the query, degrading to lexical search instead of failing."""
        if not self.library.has_embeddings:
            return None, None
        embedder = self.ensure_embedder()
        if embedder is None:
            return None, None
        try:
            return embedder.embed_one(text), None
        except Exception as exc:  # noqa: BLE001 - retrieval must survive an API outage
            return None, f"语义检索不可用，本次退回词法检索（{type(exc).__name__}: {exc}）"

    # -- helpers -------------------------------------------------------------------

    def _material(
        self,
        *,
        book: str | None,
        topic: str | None,
        limit: int = 10,
        section_filter: str | None = None,
    ) -> list[Chunk]:
        books = self.library.resolve_books([book] if book else None)

        if topic:
            vector, _warning = self._query_vector(topic)
            hits = self.library.search(
                topic,
                books=books,
                top_k=limit,
                per_book_cap=limit,
                query_vector=vector,
                rrf_k=self.rrf_k,
            )
            chunks = [chunk for chunk, _score in hits]
            if chunks:
                return chunks
            # A topic that is not in the books must fail loudly, not fall back to
            # sampling unrelated material and pretending it is about that topic.
            if not book:
                return []

        if section_filter:
            needle = section_filter.lower()
            matched = [
                chunk
                for chunk in self.library.chunks
                if chunk.book_id in {item.id for item in books} and needle in chunk.section.lower()
            ]
            if matched:
                return matched[:limit]

        if book:
            return self.library.chunks_of(books[0].id)[:limit]

        # No topic and no book: sample evenly so we never send an entire book.
        pool = [chunk for chunk in self.library.chunks if chunk.book_id in {item.id for item in books}]
        if len(pool) <= limit:
            return pool
        step = max(1, len(pool) // limit)
        return pool[::step][:limit]
