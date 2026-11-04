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
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .errors import MemoryStoreError
from .index import BM25, tokenize
from .types import Episode, Fact, MemoryHit, Profile, TopicState

__all__ = ["Memory", "MAX_FACTS", "MAX_EPISODES", "load_payload", "save_payload"]

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


def _next_id(prefix: str, existing: Iterable[str]) -> str:
    """One past the highest number in use, so deleting can never reuse an id.

    ``len(list) + 1`` reuses the id of a removed row: delete f0002 out of
    f0001..f0003 and the next fact is f0003 again, so ``--forget f0003`` would
    delete two unrelated memories at once.
    """
    highest = 0
    for identifier in existing:
        if identifier.startswith(prefix):
            suffix = identifier[len(prefix) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:04d}"


# -- snapshot parsing -------------------------------------------------------------
#
# Importing is deliberately strict: a snapshot is validated in full before any
# state changes, so a bad file never leaves the memory half-migrated.


@dataclass(slots=True)
class _Snapshot:
    """A validated, not-yet-applied memory snapshot."""

    profile: Profile
    facts: list[Fact]
    episodes: list[Episode]
    topics: list[TopicState]
    created_at: str | None
    updated_at: str | None


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemoryStoreError(f"invalid memory snapshot: {where} must be a JSON object")
    return cast(dict[str, Any], value)


def _section(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MemoryStoreError(f'invalid memory snapshot: "{key}" must be a list')
    return value


def _text(data: dict[str, Any], key: str, where: str, *, default: str = "") -> str:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must be a string")
    return value


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryStoreError(f'invalid memory snapshot: "{key}" must be a string')
    return value


def _count(data: dict[str, Any], key: str, where: str, *, default: int = 0) -> int:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must be an integer")
    if value < 0:
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must not be negative")
    return value


def _number(
    data: dict[str, Any],
    key: str,
    where: str,
    *,
    default: float,
    minimum: float | None = None,
) -> float:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must be finite")
    if minimum is not None and number < minimum:
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must be at least {minimum}")
    return number


def _string_list(data: dict[str, Any], key: str, where: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MemoryStoreError(f"invalid memory snapshot: {where}.{key} must be a list")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise MemoryStoreError(f"invalid memory snapshot: {where}.{key}[{index}] must be a string")
        cleaned = item.strip()
        if cleaned:
            items.append(cleaned)
    return items


def _collapse(text: str) -> str:
    """Collapse runs of whitespace; used for display text and topic names."""
    return " ".join(text.split())


def _topic_key(name: str) -> str:
    """Case- and whitespace-insensitive identity for a topic name."""
    return _collapse(name).casefold()


def _parse_profile(data: dict[str, Any]) -> Profile:
    where = "profile"
    return Profile(
        name=_text(data, "name", where).strip(),
        major=_text(data, "major", where).strip(),
        grade=_text(data, "grade", where).strip(),
        courses=_string_list(data, "courses", where),
        goals=_string_list(data, "goals", where),
        preferences=_string_list(data, "preferences", where),
    )


def _parse_fact(value: Any, index: int) -> Fact:
    where = f"facts[{index}]"
    data = _mapping(value, where)
    text = _collapse(_text(data, "text", where))
    if not text:
        raise MemoryStoreError(f"invalid memory snapshot: {where}.text must be a non-empty string")
    return Fact(
        id=_text(data, "id", where),
        text=text,
        topic=_collapse(_text(data, "topic", where)),
        kind=_text(data, "kind", where, default="fact"),
        ts=_text(data, "ts", where),
        weight=_number(data, "weight", where, default=1.0, minimum=0.0),
    )


def _parse_episode(value: Any, index: int) -> Episode:
    where = f"episodes[{index}]"
    data = _mapping(value, where)
    return Episode(
        id=_text(data, "id", where),
        ts=_text(data, "ts", where),
        question=_collapse(_text(data, "question", where)),
        summary=_text(data, "summary", where),
        topics=_string_list(data, "topics", where),
        books=_string_list(data, "books", where),
    )


def _parse_topic(value: Any, index: int) -> TopicState:
    where = f"topics[{index}]"
    data = _mapping(value, where)
    name = _collapse(_text(data, "name", where))
    if not name:
        raise MemoryStoreError(f"invalid memory snapshot: {where}.name must be a non-empty string")
    return TopicState(
        name=name,
        seen=_count(data, "seen", where),
        correct=_count(data, "correct", where),
        wrong=_count(data, "wrong", where),
        # Mirrors TopicState.from_dict: a falsy ease/interval means "use the default".
        ease=_number(data, "ease", where, default=2.5, minimum=0.0) or 2.5,
        interval=_number(data, "interval", where, default=1.0, minimum=0.0) or 1.0,
        last_seen=_text(data, "last_seen", where),
        due=_text(data, "due", where),
    )


def _parse_snapshot(payload: dict[str, Any]) -> _Snapshot:
    raw: Any = payload
    if not isinstance(raw, dict):
        raise MemoryStoreError("invalid memory snapshot: expected a JSON object")
    data = cast(dict[str, Any], raw)

    if "version" not in data:
        raise MemoryStoreError('invalid memory snapshot: missing "version"')
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise MemoryStoreError('invalid memory snapshot: "version" must be an integer')
    if version != _FORMAT_VERSION:
        raise MemoryStoreError(
            f"unsupported memory format version {version} (this build reads version {_FORMAT_VERSION})"
        )

    profile_data = data.get("profile")
    profile = _parse_profile({} if profile_data is None else _mapping(profile_data, "profile"))
    facts = [_parse_fact(item, index) for index, item in enumerate(_section(data, "facts"))]
    episodes = [_parse_episode(item, index) for index, item in enumerate(_section(data, "episodes"))]
    topics = [_parse_topic(item, index) for index, item in enumerate(_section(data, "topics"))]
    return _Snapshot(
        profile=profile,
        facts=facts,
        episodes=episodes,
        topics=topics,
        created_at=_optional_text(data, "created_at"),
        updated_at=_optional_text(data, "updated_at"),
    )


# -- snapshot merging -------------------------------------------------------------


def _union(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    merged = list(existing)
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


def _claim_id(prefix: str, candidate: str, used: set[str]) -> str:
    """Keep a usable imported id, otherwise allocate a fresh one."""
    if candidate and candidate not in used:
        return candidate
    return _next_id(prefix, used)


def _trim_facts(facts: Iterable[Fact]) -> list[Fact]:
    kept = list(facts)
    while len(kept) > MAX_FACTS:
        kept.remove(min(kept, key=lambda item: (item.weight, item.ts)))
    return kept


def _episode_key(episode: Episode) -> tuple[str, str]:
    return (episode.ts, _collapse(episode.question))


def _earlier(first: str, second: str) -> str:
    if not first:
        return second
    if not second:
        return first
    return min(first, second)


def _later(first: str, second: str) -> str:
    if not first:
        return second
    if not second:
        return first
    return max(first, second)


def _merge_topic(existing: TopicState, incoming: TopicState) -> None:
    """Merge one topic into another without double-counting shared history.

    ``seen`` is the larger count (a topic never becomes "less seen").  For the
    quiz counts the pair with the lower mastery wins so that a merge cannot make
    the student look stronger than either device recorded; a record without any
    attempts is never allowed to erase another record's evidence.  Scheduling
    fields lean towards reviewing sooner (smaller ease/interval, earlier due),
    and ``last_seen`` keeps the most recent contact.
    """
    existing.seen = max(existing.seen, incoming.seen)

    existing_attempts = existing.correct + existing.wrong
    incoming_attempts = incoming.correct + incoming.wrong
    take_incoming = incoming_attempts and (not existing_attempts or incoming.mastery < existing.mastery)
    if take_incoming:
        existing.correct = incoming.correct
        existing.wrong = incoming.wrong

    existing.ease = min(existing.ease, incoming.ease)
    existing.interval = min(existing.interval, incoming.interval)
    existing.last_seen = _later(existing.last_seen, incoming.last_seen)
    existing.due = _earlier(existing.due, incoming.due)


def load_payload(path: str | Path) -> dict[str, Any]:
    """Read a JSON snapshot from ``path``.

    This is the file half of the CLI/Web import flow: :meth:`Memory.import_dict`
    itself never touches the disk, so callers read the file here, hand the
    object to ``import_dict``, then call :meth:`Memory.save` to persist.

    Raises:
        MemoryStoreError: the file is unreadable, not valid JSON, or not a JSON object.
    """
    target = Path(path)
    try:
        text = target.read_text("utf-8")
    except OSError as exc:
        raise MemoryStoreError(f"cannot read memory file {target}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MemoryStoreError(f"corrupted memory file {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise MemoryStoreError(f"invalid memory file {target}: expected a JSON object")
    return cast(dict[str, Any], data)


def save_payload(path: str | Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as UTF-8 JSON, replacing ``path`` atomically.

    Companion to :func:`load_payload`; the payload itself is not validated here
    beyond being JSON-serialisable because :meth:`Memory.export_dict` always
    produces a valid snapshot.

    Raises:
        MemoryStoreError: the payload cannot be serialised or the file cannot be written.
    """
    target = Path(path)
    try:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise MemoryStoreError(f"memory snapshot is not JSON serializable: {exc}") from exc
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_text(serialized + "\n", "utf-8")
        temporary.replace(target)
    except OSError as exc:
        raise MemoryStoreError(f"cannot write memory file {target}: {exc}") from exc


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
        self.last_save_error: str | None = None
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

    def save(self) -> bool:
        """Persist to disk, returning False when the data directory is not writable.

        Callers that must report the failure (the web UI's "clear memory") check
        the result; answering a question never fails because memory could not be
        written -- the episode simply is not there on the next launch.
        """
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
        try:
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        except OSError as exc:
            self.last_save_error = str(exc)
            print(f"[memory] 无法保存到 {self.path}: {exc}", file=sys.stderr, flush=True)
            return False
        self.last_save_error = None
        return True

    # -- import / export -----------------------------------------------------------

    def export_dict(self) -> dict[str, Any]:
        """Return the complete memory as a JSON-serialisable snapshot.

        The shape is identical to the ``.studybuddy/memory.json`` file written
        by :meth:`save`: ``version``, ``created_at``, ``updated_at``,
        ``profile``, ``facts``, ``episodes`` and ``topics``.  The returned
        object is a copy, so mutating it never changes this memory.
        """
        return {
            "version": _FORMAT_VERSION,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "profile": self.profile.to_dict(),
            "facts": [fact.to_dict() for fact in self.facts],
            "episodes": [episode.to_dict() for episode in self.episodes],
            "topics": [topic.to_dict() for topic in self.topics.values()],
        }

    def import_dict(self, payload: dict[str, Any], *, merge: bool = True) -> dict[str, int]:
        """Apply a snapshot produced by :meth:`export_dict`.

        The snapshot is validated completely before anything changes, so an
        invalid payload raises :class:`MemoryStoreError` and leaves this memory
        untouched.  This method only updates in-memory state; it never writes
        the file -- call :meth:`save` afterwards to persist and refresh
        ``updated_at``.

        ``merge=True`` (default) only fills gaps, so importing the same file
        twice is harmless:

        * profile: blank local fields are filled from the snapshot and list
          fields are unioned (local order first); non-empty local values win.
        * facts: an entry whose whitespace-normalised text already exists is
          skipped (not reinforced); new entries are appended and colliding ids
          are renumbered.
        * episodes: an entry with the same ``(ts, question)`` pair is skipped
          (ids are local, so two devices can reuse the same id for different
          questions); new entries are appended and colliding ids renumbered.
        * topics: matched case- and whitespace-insensitively.  A new name is
          added with the snapshot spelling; an existing topic keeps its local
          spelling.  ``seen`` takes the larger count, while ``(correct, wrong)``
          takes the pair with the lower mastery (a record with no attempts never
          erases evidence; local wins a tie).  ``ease``/``interval`` take the
          smaller value, ``due`` the earlier one and ``last_seen`` the later one
          -- i.e. a merge leans towards reviewing sooner.
        * a completely empty memory also adopts the snapshot timestamps so a
          migration round-trips exactly.

        ``merge=False`` replaces ``profile``, ``facts``, ``episodes`` and
        ``topics`` (plus the snapshot timestamps when present).

        Capacity limits apply after loading: at most :data:`MAX_FACTS` facts
        (dropping the weakest/oldest, like :meth:`add_fact`) and
        :data:`MAX_EPISODES` episodes (keeping the newest, like
        :meth:`record_episode`).  The returned counts describe accepted
        entries before that trimming.

        Returns:
            A dict with ``facts_added``, ``episodes_added``, ``topics_added``
            and ``topics_updated``.  In replace mode ``topics_updated`` is
            always ``0`` and the ``*_added`` values are the number of distinct
            entries loaded.
        """
        snapshot = _parse_snapshot(payload)
        if merge:
            return self._apply_merge(snapshot)
        return self._apply_replace(snapshot)

    def _is_pristine(self) -> bool:
        return (
            not self.facts
            and not self.episodes
            and not self.topics
            and not any(
                (
                    self.profile.name,
                    self.profile.major,
                    self.profile.grade,
                    self.profile.courses,
                    self.profile.goals,
                    self.profile.preferences,
                )
            )
        )

    def _merge_profile(self, incoming: Profile) -> None:
        if not self.profile.name:
            self.profile.name = incoming.name
        if not self.profile.major:
            self.profile.major = incoming.major
        if not self.profile.grade:
            self.profile.grade = incoming.grade
        self.profile.courses = _union(self.profile.courses, incoming.courses)
        self.profile.goals = _union(self.profile.goals, incoming.goals)
        self.profile.preferences = _union(self.profile.preferences, incoming.preferences)

    def _apply_merge(self, snapshot: _Snapshot) -> dict[str, int]:
        counts = {"facts_added": 0, "episodes_added": 0, "topics_added": 0, "topics_updated": 0}

        if self._is_pristine():
            if snapshot.created_at:
                self.created_at = snapshot.created_at
            if snapshot.updated_at:
                self.updated_at = snapshot.updated_at

        self._merge_profile(snapshot.profile)

        seen_texts = {_collapse(fact.text) for fact in self.facts}
        used_fact_ids = {fact.id for fact in self.facts if fact.id}
        facts = list(self.facts)
        for fact in snapshot.facts:
            normalized = _collapse(fact.text)
            if normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            fact.id = _claim_id("f", fact.id, used_fact_ids)
            used_fact_ids.add(fact.id)
            facts.append(fact)
            counts["facts_added"] += 1
        self.facts = _trim_facts(facts)

        used_episode_ids = {episode.id for episode in self.episodes if episode.id}
        episode_keys = {_episode_key(episode) for episode in self.episodes}
        episodes = list(self.episodes)
        for episode in snapshot.episodes:
            if _episode_key(episode) in episode_keys:
                continue
            episode.id = _claim_id("e", episode.id, used_episode_ids)
            used_episode_ids.add(episode.id)
            episode_keys.add(_episode_key(episode))
            episodes.append(episode)
            counts["episodes_added"] += 1
        self.episodes = episodes[-MAX_EPISODES:]

        topic_keys = {_topic_key(name): name for name in self.topics}
        for topic in snapshot.topics:
            existing = topic_keys.get(_topic_key(topic.name))
            if existing is None:
                self.topics[topic.name] = topic
                topic_keys[_topic_key(topic.name)] = topic.name
                counts["topics_added"] += 1
            else:
                _merge_topic(self.topics[existing], topic)
                counts["topics_updated"] += 1
        return counts

    def _apply_replace(self, snapshot: _Snapshot) -> dict[str, int]:
        self.profile = snapshot.profile

        used_fact_ids: set[str] = set()
        facts: list[Fact] = []
        for fact in snapshot.facts:
            fact.id = _claim_id("f", fact.id, used_fact_ids)
            used_fact_ids.add(fact.id)
            facts.append(fact)
        self.facts = _trim_facts(facts)

        used_episode_ids: set[str] = set()
        episodes: list[Episode] = []
        for episode in snapshot.episodes:
            episode.id = _claim_id("e", episode.id, used_episode_ids)
            used_episode_ids.add(episode.id)
            episodes.append(episode)
        self.episodes = episodes[-MAX_EPISODES:]

        topics: dict[str, TopicState] = {}
        topic_keys: dict[str, str] = {}
        for topic in snapshot.topics:
            existing = topic_keys.get(_topic_key(topic.name))
            if existing is None:
                topics[topic.name] = topic
                topic_keys[_topic_key(topic.name)] = topic.name
            else:
                _merge_topic(topics[existing], topic)
        self.topics = topics

        if snapshot.created_at:
            self.created_at = snapshot.created_at
        if snapshot.updated_at:
            self.updated_at = snapshot.updated_at
        return {
            "facts_added": len(snapshot.facts),
            "episodes_added": len(snapshot.episodes),
            "topics_added": len(self.topics),
            "topics_updated": 0,
        }

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
            id=_next_id("f", (existing.id for existing in self.facts)),
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
        """Remove one fact by id; a duplicated legacy id still deletes exactly one row."""
        for index, fact in enumerate(self.facts):
            if fact.id == fact_id:
                del self.facts[index]
                self.save()
                return True
        return False

    def record_episode(
        self,
        question: str,
        *,
        summary: str = "",
        topics: Iterable[str] | None = None,
        books: Iterable[str] | None = None,
    ) -> Episode:
        episode = Episode(
            id=_next_id("e", (existing.id for existing in self.episodes)),
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
