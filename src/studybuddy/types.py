"""Plain data structures shared across the library.

Nothing here talks to the network or the filesystem; these are just the shapes
that move between the agent loop, the LLM client, and your own code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_openai(cls, payload: dict[str, Any]) -> ToolCall:
        function = payload.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                # Malformed JSON is handed to the tool as a single raw argument so the
                # model gets a useful error message instead of a crash.
                arguments = {"__raw_arguments__": raw_arguments}
        else:
            arguments = dict(raw_arguments)
        return cls(
            id=str(payload.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=arguments,
        )


@dataclass(slots=True)
class Message:
    """One entry of the conversation sent to (or returned by) the model."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict[str, Any]:
        """Render this message in the OpenAI chat-completions wire format."""
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        elif self.role in ("assistant", "tool"):
            payload["content"] = "" if self.role == "tool" else None
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            payload["name"] = self.name
        return payload

    def __str__(self) -> str:
        if self.tool_calls:
            names = ", ".join(call.name for call in self.tool_calls)
            return f"[{self.role}] -> {names}"
        return f"[{self.role}] {self.content or ''}"


@dataclass(slots=True)
class Usage:
    """Token accounting, accumulated across every model call in a run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def __iadd__(self, other: Usage) -> Usage:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        return self


@dataclass(slots=True)
class LLMResponse:
    """A normalised reply from the provider."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    raw: dict[str, Any] | None = None


# --------------------------------------------------------------------------------------
# Study domain
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Book:
    """A textbook that has been ingested."""

    id: str
    title: str
    course: str = ""
    pages: int = 0
    chunks: int = 0
    source: str = ""
    added_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "course": self.course,
            "pages": self.pages,
            "chunks": self.chunks,
            "source": self.source,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Book:
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            course=str(data.get("course", "")),
            pages=int(data.get("pages") or 0),
            chunks=int(data.get("chunks") or 0),
            source=str(data.get("source", "")),
            added_at=str(data.get("added_at", "")),
        )


@dataclass(slots=True)
class Chunk:
    """A retrievable slice of a textbook, carrying everything needed to cite it."""

    id: str
    book_id: str
    book_title: str
    page: int
    section: str
    text: str
    course: str = ""
    nav: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "book_id": self.book_id,
            "book_title": self.book_title,
            "page": self.page,
            "section": self.section,
            "text": self.text,
            "course": self.course,
            "nav": self.nav,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chunk:
        return cls(
            id=str(data.get("id", "")),
            book_id=str(data.get("book_id", "")),
            book_title=str(data.get("book_title", "")),
            page=int(data.get("page") or 0),
            section=str(data.get("section", "")),
            text=str(data.get("text", "")),
            course=str(data.get("course", "")),
            nav=bool(data.get("nav", False)),
        )

    @property
    def citation(self) -> str:
        where = f"《{self.book_title}》" + (f" 第{self.page}页" if self.page else "")
        if self.section:
            where += f" · {self.section}"
        return where


@dataclass(slots=True)
class Source:
    """A chunk that was shown to the model, numbered for citation."""

    index: int
    chunk: Chunk
    score: float = 0.0

    @property
    def citation(self) -> str:
        return self.chunk.citation


@dataclass(slots=True)
class Answer:
    """The result of a question about the student's textbooks."""

    text: str
    sources: list[Source] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def with_citations(self) -> str:
        """Answer text followed by a numbered source list."""
        if not self.sources:
            return self.text
        lines = [self.text, "", "来源:"]
        lines.extend(f"  [{source.index}] {source.citation}" for source in self.sources)
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Profile:
    """Who the student is. Everything here is optional."""

    name: str = ""
    major: str = ""
    grade: str = ""
    courses: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "major": self.major,
            "grade": self.grade,
            "courses": list(self.courses),
            "goals": list(self.goals),
            "preferences": list(self.preferences),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        return cls(
            name=str(data.get("name", "")),
            major=str(data.get("major", "")),
            grade=str(data.get("grade", "")),
            courses=[str(item) for item in data.get("courses") or []],
            goals=[str(item) for item in data.get("goals") or []],
            preferences=[str(item) for item in data.get("preferences") or []],
        )

    def summary(self) -> str:
        parts = [f"专业: {self.major}" if self.major else "", f"年级: {self.grade}" if self.grade else ""]
        if self.courses:
            parts.append("在修课程: " + "、".join(self.courses))
        if self.goals:
            parts.append("学习目标: " + "；".join(self.goals))
        if self.preferences:
            parts.append("讲解偏好: " + "；".join(self.preferences))
        return "；".join(part for part in parts if part)


@dataclass(slots=True)
class Fact:
    """A durable, reusable observation about the student."""

    id: str
    text: str
    topic: str = ""
    kind: str = "fact"  # fact | weakness | preference | goal
    ts: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "topic": self.topic,
            "kind": self.kind,
            "ts": self.ts,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls(
            id=str(data.get("id", "")),
            text=str(data.get("text", "")),
            topic=str(data.get("topic", "")),
            kind=str(data.get("kind", "fact")),
            ts=str(data.get("ts", "")),
            weight=float(data.get("weight") or 1.0),
        )


@dataclass(slots=True)
class Episode:
    """One past study interaction, kept so later sessions can recall it."""

    id: str
    ts: str
    question: str
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    books: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "question": self.question,
            "summary": self.summary,
            "topics": list(self.topics),
            "books": list(self.books),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Episode:
        return cls(
            id=str(data.get("id", "")),
            ts=str(data.get("ts", "")),
            question=str(data.get("question", "")),
            summary=str(data.get("summary", "")),
            topics=[str(item) for item in data.get("topics") or []],
            books=[str(item) for item in data.get("books") or []],
        )


@dataclass(slots=True)
class TopicState:
    """Per-topic mastery estimate, scheduled with a light SM-2 variant."""

    name: str
    seen: int = 1
    correct: int = 0
    wrong: int = 0
    ease: float = 2.5
    interval: float = 1.0
    last_seen: str = ""
    due: str = ""

    @property
    def mastery(self) -> float:
        attempts = self.correct + self.wrong
        if not attempts:
            return 0.0
        return round(self.correct / attempts, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "seen": self.seen,
            "correct": self.correct,
            "wrong": self.wrong,
            "ease": self.ease,
            "interval": self.interval,
            "last_seen": self.last_seen,
            "due": self.due,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicState:
        return cls(
            name=str(data.get("name", "")),
            seen=int(data.get("seen") or 0),
            correct=int(data.get("correct") or 0),
            wrong=int(data.get("wrong") or 0),
            ease=float(data.get("ease") or 2.5),
            interval=float(data.get("interval") or 1.0),
            last_seen=str(data.get("last_seen") or ""),
            due=str(data.get("due") or ""),
        )


@dataclass(slots=True)
class MemoryHit:
    """A recalled memory, ranked against the current question."""

    kind: str  # fact | episode | topic
    text: str
    score: float = 0.0
    ts: str = ""
