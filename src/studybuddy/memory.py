"""Long-term memory: what the student is like, what they struggled with, what to review.

Three things are remembered, and they serve different purposes:

``profile``   Who the student is (major, courses, preferences). Injected into every prompt.
``facts``     Durable observations ("prefers derivations before formulas").
``episodes``  What was asked before, so a later session can say "last week you asked...".
``topics``    A per-topic mastery estimate scheduled with a light SM-2 variant, so the
              assistant can tell the student what to review *today* rather than what is
              merely interesting.

Everything is a plain JSON file under ``.studybuddy/memory.json`` -- auditable, portable,
and easy to delete if the student wants to forget.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from .errors import MemoryStoreError
from .index import BM25, tokenize
from .types import Episode, Fact, MemoryHit, Profile, TopicState

__all__ = ["Memory", "MAX_FACTS", "MAX_EPISODES"]

MAX_FACTS = 300
MAX_EPISODES = 500
_FORMAT_VERSION = 1

_KIND_LABELS = {
    "fact": "记忆",
    "weakness": "薄弱点",
    "preference": "偏好",
    "goal": "目标",
}


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(moment: str) -> datetime | None:
    try:
        return datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return None


class Memory:
    """The student's persistent learning history."""

    def __init__(self, root: str | Path = ".studybuddy") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "memory.json"
        self.profile = Profile()
        self.facts: list[Fact] = []
        self.episodes: list[Episode] = []
        self.topics: dict[str, TopicState] = {}
        self.created_at = _iso(_now())
        self.updated_at = self.created_at
        self.load()

    # -- persistence ---------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(f"corrupted memory file {self.path}: {exc}") from exc

        self.profile = Profile.from_dict(data.get("profile") or {})
        self.facts = [Fact.from_dict(item) for item in data.get("facts") or []]
        self.episodes = [Episode.from_dict(item) for item in data.get("episodes") or []]
        self.topics = {
            item["name"]: TopicState.from_dict(item)
            for item in data.get("topics") or []
            if isinstance(item, dict) and item.get("name")
        }
        self.created_at = str(data.get("created_at") or self.created_at)
        self.updated_at = str(data.get("updated_at") or self.updated_at)

    def save(self) -> None:
        self.updated_at = _iso(_now())
        payload = {
            "version": _FORMAT_VERSION,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "profile": self.profile.to_dict(),
            "facts": [fact.to_dict() for fact in self.facts],
            "episodes": [episode.to_dict() for episode in self.episodes],
            "topics": [topic.to_dict() for topic in self.topics.values()],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    # -- profile -------------------------------------------------------------------

    def set_profile(
        self,
        *,
        name: str | None = None,
        major: str | None = None,
        grade: str | None = None,
        courses: Iterable[str] | None = None,
        goals: Iterable[str] | None = None,
        preferences: Iterable[str] | None = None,
    ) -> Profile:
        if name is not None:
            self.profile.name = name.strip()
        if major is not None:
            self.profile.major = major.strip()
        if grade is not None:
            self.profile.grade = grade.strip()
        if courses is not None:
            self.profile.courses = [item.strip() for item in courses if item.strip()]
        if goals is not None:
            self.profile.goals = [item.strip() for item in goals if item.strip()]
        if preferences is not None:
            self.profile.preferences = [item.strip() for item in preferences if item.strip()]
        self.save()
        return self.profile

    # -- facts & episodes ----------------------------------------------------------

    def add_fact(self, text: str, *, topic: str = "", kind: str = "fact", weight: float = 1.0) -> Fact:
        """Remember a durable observation, skipping near-duplicates."""
        cleaned = " ".join(text.split()).strip()
        if not cleaned:
            raise ValueError("fact text must not be empty")
        if kind not in _KIND_LABELS:
            kind = "fact"

        for existing in self.facts:
            if existing.text == cleaned:
                existing.weight = min(2.0, existing.weight + 0.2)
                self.save()
                return existing

        fact = Fact(
            id=f"f{len(self.facts) + 1:04d}",
            text=cleaned,
            topic=topic.strip(),
            kind=kind,
            ts=_iso(_now()),
            weight=weight,
        )
        self.facts.append(fact)
        if len(self.facts) > MAX_FACTS:
            # Drop the oldest, least reinforced memory first.
            drop = min(self.facts, key=lambda item: (item.weight, item.ts))
            self.facts.remove(drop)
        self.save()
        return fact

    def remove_fact(self, fact_id: str) -> bool:
        before = len(self.facts)
        self.facts = [fact for fact in self.facts if fact.id != fact_id]
        changed = len(self.facts) != before
        if changed:
            self.save()
        return changed

    def record_episode(
        self,
        question: str,
        *,
        summary: str = "",
        topics: Iterable[str] | None = None,
        books: Iterable[str] | None = None,
    ) -> Episode:
        episode = Episode(
            id=f"e{len(self.episodes) + 1:04d}",
            ts=_iso(_now()),
            question=" ".join(question.split()),
            summary=summary[:400],
            topics=[item.strip() for item in topics or () if item and item.strip()],
            books=[item.strip() for item in books or () if item and item.strip()],
        )
        self.episodes.append(episode)
        if len(self.episodes) > MAX_EPISODES:
            self.episodes = self.episodes[-MAX_EPISODES:]
        self.save()
        return episode

    # -- topics & spaced repetition ------------------------------------------------

    def note_topic(self, name: str) -> TopicState:
        """Record that a topic was encountered (asked about, read, or quizzed)."""
        cleaned = " ".join(name.split()).strip()
        if not cleaned:
            raise ValueError("topic name must not be empty")

        existing = self.topics.get(cleaned)
        if existing is None:
            state = TopicState(
                name=cleaned,
                seen=1,
                last_seen=_iso(_now()),
                due=_iso(_now() + timedelta(days=1)),
            )
            self.topics[cleaned] = state
            self.save()
            return state

        existing.seen += 1
        existing.last_seen = _iso(_now())
        self.save()
        return existing

    def grade_topic(self, name: str, *, correct: bool) -> TopicState:
        """Update a topic after a quiz answer using a light SM-2 schedule."""
        state = self.topics.get(name) or self.note_topic(name)
        now = _now()

        if correct:
            state.correct += 1
            state.ease = min(2.8, round(state.ease + 0.1, 3))
            # SM-2 style: 1 day, then 6 days, then multiply by the ease factor.
            if state.correct == 1:
                state.interval = 1.0
            elif state.correct == 2:
                state.interval = 6.0
            else:
                state.interval = round(state.interval * state.ease, 2)
        else:
            state.wrong += 1
            state.ease = max(1.3, round(state.ease - 0.2, 3))
            state.interval = 1.0

        state.interval = max(1.0, min(state.interval, 60.0))
        state.last_seen = _iso(now)
        state.due = _iso(now + timedelta(days=state.interval))
        self.save()
        return state

    def due_topics(self, *, limit: int = 10, now: datetime | None = None) -> list[TopicState]:
        moment = now or _now()
        due: list[TopicState] = []
        for topic in self.topics.values():
            due_at = _parse(topic.due)
            if due_at is None or due_at <= moment:
                due.append(topic)
        due.sort(key=lambda item: (item.due or "", -item.wrong, item.ease))
        return due[:limit]

    # -- recall --------------------------------------------------------------------

    def _documents(self) -> tuple[list[list[str]], list[MemoryHit]]:
        documents: list[list[str]] = []
        hits: list[MemoryHit] = []

        for fact in self.facts:
            documents.append(tokenize(f"{fact.topic} {fact.text}"))
            label = _KIND_LABELS.get(fact.kind, "记忆")
            hits.append(MemoryHit(kind="fact", text=f"[{label}] {fact.text}", ts=fact.ts))

        for episode in self.episodes:
            joined = " ".join(episode.topics)
            documents.append(tokenize(f"{episode.question} {episode.summary} {joined}"))
            hits.append(
                MemoryHit(
                    kind="episode",
                    text=f"[上次问过] {episode.question}",
                    ts=episode.ts,
                )
            )

        for topic in self.topics.values():
            documents.append(tokenize(topic.name))
            hits.append(
                MemoryHit(
                    kind="topic",
                    text=f"[话题] {topic.name}｜掌握度 {topic.mastery:.0%}｜见面 {topic.seen} 次",
                    ts=topic.last_seen,
                )
            )

        return documents, hits

    def recall(self, query: str, *, limit: int = 4, min_score: float = 0.0) -> list[MemoryHit]:
        """Return memories relevant to ``query``, most relevant first."""
        documents, hits = self._documents()
        if not documents or not query.strip():
            return []

        index = BM25(documents)
        ranked = index.top_k(query, k=limit, min_score=min_score)
        recalled: list[MemoryHit] = []
        for position, score in ranked:
            hit = hits[position]
            hit.score = round(score, 3)
            recalled.append(hit)
        return recalled

    # -- prompt helpers ------------------------------------------------------------

    def context_block(self, query: str, *, limit: int = 4) -> str:
        """A compact block for the system/user prompt, or an empty string."""
        lines: list[str] = []

        profile_summary = self.profile.summary()
        if profile_summary:
            lines.append(f"学生画像: {profile_summary}")

        recalled = self.recall(query, limit=limit)
        if recalled:
            lines.append("关于这名学生的记忆:")
            lines.extend(f"  - {hit.text}" for hit in recalled)

        due = self.due_topics(limit=3)
        if due:
            names = "、".join(topic.name for topic in due)
            lines.append(f"今天适合复习: {names}")

        return "\n".join(lines)

    # -- reporting -----------------------------------------------------------------

    def digest(self) -> str:
        """Human-readable summary for ``studybuddy memory``."""
        lines = [f"记忆文件: {self.path}", f"创建于 {self.created_at}｜更新于 {self.updated_at}", ""]

        profile_summary = self.profile.summary()
        lines.append(f"画像: {profile_summary or '（还没填，用 studybuddy profile 设置）'}")

        if self.facts:
            lines.append("")
            lines.append(f"记住了 {len(self.facts)} 条:")
            lines.extend(
                f"  {fact.id} {_KIND_LABELS.get(fact.kind, '记忆')}: {fact.text}" for fact in self.facts[-10:]
            )

        if self.topics:
            lines.append("")
            lines.append(f"在跟进的 {len(self.topics)} 个话题:")
            ordered = sorted(self.topics.values(), key=lambda item: (item.wrong, -item.seen), reverse=True)
            for topic in ordered[:10]:
                due = "已到期" if topic.due and topic.due <= _iso(_now()) else f"下次 {topic.due[:10] or '—'}"
                lines.append(
                    f"  {topic.name}: 见 {topic.seen} 次, 对 {topic.correct} / 错 {topic.wrong}, "
                    f"掌握度 {topic.mastery:.0%}, {due}"
                )

        if self.episodes:
            lines.append("")
            lines.append(f"历史提问 {len(self.episodes)} 条（最近 5 条）:")
            lines.extend(f"  {episode.ts[:16]}  {episode.question[:60]}" for episode in self.episodes[-5:])

        return "\n".join(lines)

    def stats(self) -> dict[str, object]:
        return {
            "facts": len(self.facts),
            "episodes": len(self.episodes),
            "topics": len(self.topics),
            "due": len(self.due_topics(limit=1000)),
            "path": str(self.path),
        }
