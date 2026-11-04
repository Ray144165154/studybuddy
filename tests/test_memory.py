"""Long-term memory: facts, recall, topics and the spaced-repetition schedule."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from studybuddy import Memory, MemoryStoreError
from studybuddy.memory import MAX_EPISODES, MAX_FACTS, load_payload, save_payload


def _snapshot(version: int = 1, **sections: Any) -> dict[str, Any]:
    """A minimal but valid memory snapshot, plus whatever sections a test needs."""
    payload: dict[str, Any] = {
        "version": version,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-02T00:00:00",
        "profile": {},
        "facts": [],
        "episodes": [],
        "topics": [],
    }
    payload.update(sections)
    return payload


def _content(payload: dict[str, Any]) -> dict[str, Any]:
    """The semantic part of a snapshot, without wall-clock bookkeeping."""
    return {key: payload[key] for key in ("profile", "facts", "episodes", "topics")}


def _topic(
    name: str,
    *,
    seen: int = 1,
    correct: int = 0,
    wrong: int = 0,
    ease: float = 2.5,
    interval: float = 1.0,
    last_seen: str = "2024-01-01T00:00:00",
    due: str = "2024-01-02T00:00:00",
) -> dict[str, Any]:
    return {
        "name": name,
        "seen": seen,
        "correct": correct,
        "wrong": wrong,
        "ease": ease,
        "interval": interval,
        "last_seen": last_seen,
        "due": due,
    }


def _fact(text: str, *, id: str = "", weight: float = 1.0, ts: str = "2024-01-01T00:00:00") -> dict[str, Any]:
    return {"id": id, "text": text, "topic": "", "kind": "fact", "ts": ts, "weight": weight}


def _episode(
    question: str,
    *,
    id: str = "",
    ts: str = "2024-01-01T00:00:00",
    summary: str = "",
    topics: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "ts": ts,
        "question": question,
        "summary": summary,
        "topics": topics or [],
        "books": [],
    }


class TestMemoryBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.memory = Memory(self.root)


class TestProfile(TestMemoryBase):
    def test_defaults_are_empty(self) -> None:
        self.assertEqual(self.memory.profile.major, "")
        self.assertEqual(self.memory.profile.courses, [])

    def test_set_profile_persists(self) -> None:
        self.memory.set_profile(major="计算机科学与技术", grade="大二", courses=["高等数学", "线性代数"])
        reopened = Memory(self.root)
        self.assertEqual(reopened.profile.major, "计算机科学与技术")
        self.assertEqual(reopened.profile.courses, ["高等数学", "线性代数"])

    def test_partial_update_keeps_other_fields(self) -> None:
        self.memory.set_profile(major="物理", courses=["力学"])
        self.memory.set_profile(grade="大一")
        self.assertEqual(self.memory.profile.major, "物理")
        self.assertEqual(self.memory.profile.grade, "大一")

    def test_summary_text(self) -> None:
        self.memory.set_profile(major="数学", courses=["数分"], preferences=["先看推导"])
        summary = self.memory.profile.summary()
        self.assertIn("数学", summary)
        self.assertIn("数分", summary)
        self.assertIn("先看推导", summary)


class TestFacts(TestMemoryBase):
    def test_add_fact(self) -> None:
        fact = self.memory.add_fact("偏好先看公式再看例子", kind="preference", topic="导数")
        self.assertEqual(fact.kind, "preference")
        self.assertEqual(len(self.memory.facts), 1)

    def test_duplicate_fact_reinforces(self) -> None:
        self.memory.add_fact("容易混淆中值定理")
        self.memory.add_fact("容易混淆中值定理")
        self.assertEqual(len(self.memory.facts), 1)
        self.assertGreater(self.memory.facts[0].weight, 1.0)

    def test_unknown_kind_falls_back(self) -> None:
        self.assertEqual(self.memory.add_fact("x", kind="nonsense").kind, "fact")

    def test_empty_fact_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.add_fact("   ")

    def test_remove_fact(self) -> None:
        fact = self.memory.add_fact("待删除")
        self.assertTrue(self.memory.remove_fact(fact.id))
        self.assertFalse(self.memory.remove_fact(fact.id))
        self.assertEqual(self.memory.facts, [])


class TestRecall(TestMemoryBase):
    def test_recall_ranks_relevant_memory(self) -> None:
        self.memory.add_fact("导数部分基础薄弱，经常忘记定义", kind="weakness", topic="导数")
        self.memory.add_fact("喜欢用编程例子理解数学", kind="preference", topic="编程")
        hits = self.memory.recall("导数怎么求")
        self.assertTrue(hits)
        self.assertIn("导数", hits[0].text)

    def test_recall_includes_episodes(self) -> None:
        self.memory.record_episode("拉格朗日中值定理怎么证明", summary="讲了一遍证明", topics=["中值定理"])
        hits = self.memory.recall("中值定理")
        self.assertTrue(any(hit.kind == "episode" for hit in hits))

    def test_recall_empty_query(self) -> None:
        self.memory.add_fact("某条记忆")
        self.assertEqual(self.memory.recall(""), [])

    def test_recall_on_empty_memory(self) -> None:
        self.assertEqual(self.memory.recall("导数"), [])

    def test_episode_persists(self) -> None:
        self.memory.record_episode("问题", summary="摘要", topics=["极限"], books=["高等数学"])
        reopened = Memory(self.root)
        self.assertEqual(len(reopened.episodes), 1)
        self.assertEqual(reopened.episodes[0].topics, ["极限"])


class TestTopics(TestMemoryBase):
    def test_note_topic_increments(self) -> None:
        self.memory.note_topic("极限")
        self.assertEqual(self.memory.topics["极限"].seen, 1)
        self.memory.note_topic("极限")
        self.assertEqual(self.memory.topics["极限"].seen, 2)

    def test_empty_topic_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.note_topic("  ")

    def test_first_correct_is_one_day(self) -> None:
        state = self.memory.grade_topic("极限", correct=True)
        self.assertEqual(state.interval, 1.0)
        self.assertEqual(state.mastery, 1.0)

    def test_second_correct_jumps_to_six_days(self) -> None:
        self.memory.grade_topic("极限", correct=True)
        state = self.memory.grade_topic("极限", correct=True)
        self.assertEqual(state.interval, 6.0)

    def test_later_correct_multiplies_by_ease(self) -> None:
        for _ in range(2):
            self.memory.grade_topic("极限", correct=True)
        state = self.memory.grade_topic("极限", correct=True)
        self.assertGreater(state.interval, 6.0)
        self.assertAlmostEqual(state.interval, round(6.0 * state.ease, 2))

    def test_wrong_resets_interval_and_ease(self) -> None:
        for _ in range(3):
            self.memory.grade_topic("极限", correct=True)
        ease_before = self.memory.topics["极限"].ease
        state = self.memory.grade_topic("极限", correct=False)
        self.assertEqual(state.interval, 1.0)
        self.assertLess(state.ease, ease_before)
        self.assertGreaterEqual(state.ease, 1.3)

    def test_due_topics(self) -> None:
        self.memory.note_topic("极限")
        self.memory.topics["极限"].due = "2000-01-01T00:00:00"
        due = self.memory.due_topics()
        self.assertEqual([topic.name for topic in due], ["极限"])

    def test_not_due_topic_is_excluded(self) -> None:
        self.memory.note_topic("极限")
        self.assertEqual(self.memory.due_topics(), [])


class TestPromptHelpers(TestMemoryBase):
    def test_context_block_contains_profile_and_memory(self) -> None:
        self.memory.set_profile(major="计算机", courses=["高等数学"])
        self.memory.add_fact("导数基础薄弱", kind="weakness", topic="导数")
        block = self.memory.context_block("导数")
        self.assertIn("计算机", block)
        self.assertIn("导数基础薄弱", block)

    def test_context_block_empty(self) -> None:
        self.assertEqual(self.memory.context_block("导数"), "")

    def test_context_block_mentions_due_topics(self) -> None:
        self.memory.note_topic("极限")
        self.memory.topics["极限"].due = "2000-01-01T00:00:00"
        self.assertIn("今天适合复习", self.memory.context_block("极限"))

    def test_digest(self) -> None:
        self.memory.set_profile(major="数学")
        self.memory.add_fact("记忆一条")
        digest = self.memory.digest()
        self.assertIn("数学", digest)
        self.assertIn("记忆一条", digest)

    def test_stats(self) -> None:
        self.memory.add_fact("a")
        self.memory.record_episode("q")
        stats = self.memory.stats()
        self.assertEqual(stats["facts"], 1)
        self.assertEqual(stats["episodes"], 1)


class TestExportDict(TestMemoryBase):
    def _populate(self) -> None:
        self.memory.set_profile(name="小明", major="数学", courses=["高等数学"])
        self.memory.add_fact("导数基础薄弱", kind="weakness", topic="导数")
        self.memory.record_episode("拉格朗日中值定理怎么证明", summary="讲了一遍", topics=["中值定理"])
        self.memory.note_topic("极限")

    def test_export_has_documented_shape(self) -> None:
        payload = self.memory.export_dict()
        self.assertEqual(
            set(payload),
            {"version", "created_at", "updated_at", "profile", "facts", "episodes", "topics"},
        )
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["facts"], [])
        self.assertEqual(payload["episodes"], [])
        self.assertEqual(payload["topics"], [])
        self.assertEqual(payload["profile"]["name"], "")

    def test_export_matches_the_saved_file(self) -> None:
        self._populate()
        self.assertTrue(self.memory.save())
        on_disk = json.loads(self.memory.path.read_text("utf-8"))
        self.assertEqual(self.memory.export_dict(), on_disk)

    def test_export_is_json_serializable(self) -> None:
        self._populate()
        encoded = json.dumps(self.memory.export_dict(), ensure_ascii=False)
        self.assertIn("小明", encoded)

    def test_export_returns_a_copy(self) -> None:
        self._populate()
        payload = self.memory.export_dict()
        payload["profile"]["name"] = "被改掉"
        payload["facts"].append(_fact("凭空多出来"))
        self.assertEqual(self.memory.profile.name, "小明")
        self.assertEqual(len(self.memory.facts), 1)


class TestExportImportRoundTrip(TestMemoryBase):
    def _populate(self, memory: Memory) -> None:
        memory.set_profile(
            name="小明",
            major="计算机科学与技术",
            grade="大二",
            courses=["高等数学", "线性代数"],
            goals=["考研"],
            preferences=["先看推导"],
        )
        memory.add_fact("导数基础薄弱", kind="weakness", topic="导数")
        memory.add_fact("喜欢用编程例子理解数学", kind="preference", topic="编程")
        memory.record_episode("中值定理怎么证明", summary="讲了一遍证明", topics=["中值定理"])
        memory.note_topic("极限")
        memory.grade_topic("极限", correct=True)
        memory.note_topic("导数")
        memory.grade_topic("导数", correct=False)

    def test_replace_round_trip_is_exact(self) -> None:
        source = Memory(self.root / "source")
        self._populate(source)
        payload = source.export_dict()

        target = Memory(self.root / "target")
        counts = target.import_dict(json.loads(json.dumps(payload)), merge=False)

        self.assertEqual(target.export_dict(), payload)
        self.assertEqual(counts["facts_added"], 2)
        self.assertEqual(counts["episodes_added"], 1)
        self.assertEqual(counts["topics_added"], 2)
        self.assertEqual(counts["topics_updated"], 0)

    def test_merge_into_an_empty_memory_round_trips_exactly(self) -> None:
        source = Memory(self.root / "source")
        self._populate(source)
        payload = source.export_dict()

        target = Memory(self.root / "target")
        target.import_dict(payload)  # merge=True is the default

        self.assertEqual(target.export_dict(), payload)

    def test_import_then_save_survives_a_reload(self) -> None:
        source = Memory(self.root / "source")
        self._populate(source)
        payload = source.export_dict()

        target = Memory(self.root / "target")
        target.import_dict(payload, merge=False)
        self.assertTrue(target.save())

        reopened = Memory(target.root)
        self.assertEqual(_content(reopened.export_dict()), _content(payload))
        self.assertEqual(reopened.created_at, payload["created_at"])

    def test_empty_memory_round_trip(self) -> None:
        source = Memory(self.root / "source")
        payload = source.export_dict()
        target = Memory(self.root / "target")
        self.assertEqual(
            target.import_dict(payload, merge=False),
            {"facts_added": 0, "episodes_added": 0, "topics_added": 0, "topics_updated": 0},
        )
        self.assertEqual(target.export_dict(), payload)


class TestImportMerge(TestMemoryBase):
    def test_merge_fills_blank_profile_fields_and_keeps_existing_ones(self) -> None:
        self.memory.set_profile(name="小明", major="数学")
        payload = _snapshot(
            profile={
                "name": "小红",
                "major": "物理",
                "grade": "大二",
                "courses": [],
                "goals": [],
                "preferences": [],
            }
        )
        self.memory.import_dict(payload)
        self.assertEqual(self.memory.profile.name, "小明")
        self.assertEqual(self.memory.profile.major, "数学")
        self.assertEqual(self.memory.profile.grade, "大二")

    def test_merge_unions_profile_lists_without_duplicates(self) -> None:
        self.memory.set_profile(courses=["高等数学"], preferences=["先看推导"])
        payload = _snapshot(
            profile={
                "name": "",
                "major": "",
                "grade": "",
                "courses": ["高等数学", "线性代数"],
                "goals": ["考研"],
                "preferences": ["多举例子"],
            }
        )
        self.memory.import_dict(payload)
        self.assertEqual(self.memory.profile.courses, ["高等数学", "线性代数"])
        self.assertEqual(self.memory.profile.goals, ["考研"])
        self.assertEqual(self.memory.profile.preferences, ["先看推导", "多举例子"])

    def test_merge_adds_new_facts_and_skips_duplicate_text(self) -> None:
        existing = self.memory.add_fact("喜欢 先看 推导")
        payload = _snapshot(
            facts=[
                _fact("喜欢   先看   推导"),  # whitespace-normalised duplicate
                _fact("边界条件容易错", weight=1.5),
            ]
        )
        counts = self.memory.import_dict(payload)
        self.assertEqual(counts["facts_added"], 1)
        self.assertEqual(len(self.memory.facts), 2)
        self.assertEqual(self.memory.facts[0].text, "喜欢 先看 推导")
        self.assertEqual(self.memory.facts[0].weight, existing.weight)  # not reinforced
        self.assertEqual(self.memory.facts[1].text, "边界条件容易错")

    def test_merge_renumbers_colliding_fact_ids(self) -> None:
        self.memory.add_fact("本地事实")  # f0001
        payload = _snapshot(
            facts=[
                _fact("远端事实", id="f0001"),
                _fact("另一条事实", id=""),
            ]
        )
        counts = self.memory.import_dict(payload)
        self.assertEqual(counts["facts_added"], 2)
        self.assertEqual([fact.id for fact in self.memory.facts], ["f0001", "f0002", "f0003"])
        self.assertEqual([fact.text for fact in self.memory.facts], ["本地事实", "远端事实", "另一条事实"])

    def test_merge_appends_only_unseen_episodes(self) -> None:
        self.memory.record_episode("问题A", summary="摘要", topics=["极限"])
        local = self.memory.episodes[0]
        payload = _snapshot(
            episodes=[
                local.to_dict(),  # same id and content
                _episode("问题A ", ts=local.ts),  # same (ts, question) after normalisation
                _episode("新问题", id="e0001"),  # id collision, different content
            ]
        )
        counts = self.memory.import_dict(payload)
        self.assertEqual(counts["episodes_added"], 1)
        self.assertEqual(len(self.memory.episodes), 2)
        self.assertEqual(self.memory.episodes[1].id, "e0002")
        self.assertEqual(self.memory.episodes[1].question, "新问题")

    def test_merge_matches_topics_case_and_whitespace_insensitively(self) -> None:
        self.memory.note_topic("Linear Algebra")
        payload = _snapshot(topics=[_topic(" linear   algebra ", seen=3, correct=1, wrong=1)])
        counts = self.memory.import_dict(payload)
        self.assertEqual(counts["topics_added"], 0)
        self.assertEqual(counts["topics_updated"], 1)
        self.assertEqual(list(self.memory.topics), ["Linear Algebra"])
        self.assertEqual(self.memory.topics["Linear Algebra"].seen, 3)

    def test_merge_takes_the_more_conservative_mastery(self) -> None:
        self.memory.note_topic("Calculus")
        for _ in range(3):
            self.memory.grade_topic("Calculus", correct=True)
        local_mastery = self.memory.topics["Calculus"].mastery
        payload = _snapshot(
            topics=[
                _topic(
                    "calculus",
                    seen=9,
                    correct=1,
                    wrong=3,
                    ease=1.8,
                    interval=1.0,
                    last_seen="2024-03-01T00:00:00",
                    due="2024-03-02T00:00:00",
                )
            ]
        )
        self.memory.import_dict(payload)
        state = self.memory.topics["Calculus"]
        self.assertEqual(state.seen, 9)  # encounters never shrink
        self.assertEqual((state.correct, state.wrong), (1, 3))  # lower mastery wins
        self.assertLess(state.mastery, local_mastery)
        self.assertEqual(state.ease, 1.8)  # more conservative schedule
        self.assertEqual(state.interval, 1.0)
        self.assertEqual(state.due, "2024-03-02T00:00:00")  # earlier due wins

    def test_merge_keeps_local_evidence_when_the_import_has_none(self) -> None:
        self.memory.grade_topic("极限", correct=True)
        self.memory.grade_topic("极限", correct=True)
        payload = _snapshot(topics=[_topic("极限", seen=5, correct=0, wrong=0)])
        self.memory.import_dict(payload)
        state = self.memory.topics["极限"]
        self.assertEqual((state.correct, state.wrong), (2, 0))
        self.assertEqual(state.mastery, 1.0)
        self.assertEqual(state.seen, 5)

    def test_merge_adopts_imported_evidence_when_local_has_none(self) -> None:
        self.memory.note_topic("极限")  # seen once, never quizzed
        payload = _snapshot(topics=[_topic("极限", seen=4, correct=3, wrong=1)])
        self.memory.import_dict(payload)
        state = self.memory.topics["极限"]
        self.assertEqual((state.correct, state.wrong), (3, 1))
        self.assertEqual(state.mastery, 0.75)
        self.assertEqual(state.seen, 4)

    def test_merge_does_not_touch_local_timestamps(self) -> None:
        self.memory.add_fact("本地事实")
        created_at = self.memory.created_at
        payload = _snapshot(facts=[_fact("远端事实")])
        self.memory.import_dict(payload)
        self.assertEqual(self.memory.created_at, created_at)

    def test_reimporting_own_snapshot_is_idempotent(self) -> None:
        self.memory.set_profile(major="数学")
        self.memory.add_fact("事实")
        self.memory.record_episode("问题")
        self.memory.grade_topic("极限", correct=True)
        before = self.memory.export_dict()

        counts = self.memory.import_dict(before)
        self.assertEqual(counts["facts_added"], 0)
        self.assertEqual(counts["episodes_added"], 0)
        self.assertEqual(counts["topics_added"], 0)
        self.assertEqual(self.memory.export_dict(), before)


class TestImportReplace(TestMemoryBase):
    def test_replace_drops_local_state_and_takes_the_snapshot(self) -> None:
        self.memory.set_profile(name="小明", major="数学")
        self.memory.add_fact("本地事实")
        self.memory.record_episode("本地问题")
        self.memory.note_topic("本地话题")

        payload = _snapshot(
            profile={
                "name": "小红",
                "major": "物理",
                "grade": "大一",
                "courses": ["力学"],
                "goals": [],
                "preferences": [],
            },
            facts=[_fact("远端事实", id="f0007")],
            episodes=[_episode("远端问题", id="e0007")],
            topics=[_topic("远端话题", seen=2, correct=1, wrong=1)],
        )
        counts = self.memory.import_dict(payload, merge=False)

        self.assertNotIn("本地事实", [fact.text for fact in self.memory.facts])
        self.assertEqual(self.memory.profile.name, "小红")
        self.assertEqual(self.memory.profile.courses, ["力学"])
        self.assertEqual([fact.id for fact in self.memory.facts], ["f0007"])
        self.assertEqual([episode.id for episode in self.memory.episodes], ["e0007"])
        self.assertEqual(list(self.memory.topics), ["远端话题"])
        self.assertEqual(
            counts, {"facts_added": 1, "episodes_added": 1, "topics_added": 1, "topics_updated": 0}
        )

    def test_replace_adopts_snapshot_timestamps(self) -> None:
        payload = _snapshot()
        payload["created_at"] = "2000-01-01T00:00:00"
        payload["updated_at"] = "2001-01-01T00:00:00"
        self.memory.import_dict(payload, merge=False)
        self.assertEqual(self.memory.created_at, "2000-01-01T00:00:00")
        self.assertEqual(self.memory.updated_at, "2001-01-01T00:00:00")

    def test_replace_with_an_empty_snapshot_clears_memory(self) -> None:
        self.memory.set_profile(major="数学")
        self.memory.add_fact("本地事实")
        self.memory.record_episode("本地问题")
        self.memory.note_topic("本地话题")

        self.memory.import_dict(_snapshot(), merge=False)

        self.assertEqual(self.memory.profile.major, "")
        self.assertEqual(self.memory.facts, [])
        self.assertEqual(self.memory.episodes, [])
        self.assertEqual(self.memory.topics, {})

    def test_duplicate_topic_spellings_in_one_snapshot_are_merged(self) -> None:
        payload = _snapshot(
            topics=[
                _topic("Calculus", seen=2, correct=2, wrong=0),
                _topic(" calculus ", seen=5, correct=1, wrong=2),
            ]
        )
        target = Memory(self.root / "target")
        counts = target.import_dict(payload, merge=False)
        self.assertEqual(counts["topics_added"], 1)
        self.assertEqual(list(target.topics), ["Calculus"])
        state = target.topics["Calculus"]
        self.assertEqual(state.seen, 5)
        self.assertEqual((state.correct, state.wrong), (1, 2))


class TestImportValidation(TestMemoryBase):
    def _valid_payload(self) -> dict[str, Any]:
        return _snapshot(
            profile={"name": "小明", "major": "数学", "grade": "", "courses": ["高数"]},
            facts=[_fact("事实", id="f0001")],
            episodes=[_episode("问题", id="e0001")],
            topics=[_topic("极限", seen=2, correct=1, wrong=1)],
        )

    def test_missing_version_is_rejected(self) -> None:
        payload = self._valid_payload()
        del payload["version"]
        with self.assertRaises(MemoryStoreError) as caught:
            self.memory.import_dict(payload)
        self.assertIn("version", str(caught.exception))

    def test_version_must_be_an_integer(self) -> None:
        for value in ("1", 1.0, True, None):
            with self.subTest(value=value):
                payload = self._valid_payload()
                payload["version"] = value
                with self.assertRaises(MemoryStoreError):
                    self.memory.import_dict(payload)

    def test_unsupported_version_reports_both_versions(self) -> None:
        payload = self._valid_payload()
        payload["version"] = 2
        with self.assertRaises(MemoryStoreError) as caught:
            self.memory.import_dict(payload)
        message = str(caught.exception)
        self.assertIn("2", message)
        self.assertIn("1", message)

    def test_payload_must_be_an_object(self) -> None:
        for value in ([], "snapshot", 3, None):
            with self.subTest(value=value), self.assertRaises(MemoryStoreError):
                self.memory.import_dict(value)  # type: ignore[arg-type]

    def test_sections_must_be_lists(self) -> None:
        for key in ("facts", "episodes", "topics"):
            with self.subTest(key=key):
                payload = self._valid_payload()
                payload[key] = {"not": "a list"}
                with self.assertRaises(MemoryStoreError):
                    self.memory.import_dict(payload)

    def test_profile_must_be_an_object(self) -> None:
        payload = self._valid_payload()
        payload["profile"] = ["not", "an", "object"]
        with self.assertRaises(MemoryStoreError):
            self.memory.import_dict(payload)

    def test_profile_field_types_are_checked(self) -> None:
        cases: list[dict[str, Any]] = [
            {"name": 5},
            {"courses": "高等数学"},
            {"courses": ["高等数学", 3]},
            {"goals": [None]},
        ]
        for profile in cases:
            with self.subTest(profile=profile):
                payload = self._valid_payload()
                payload["profile"] = profile
                with self.assertRaises(MemoryStoreError):
                    self.memory.import_dict(payload)

    def test_fact_entries_are_checked(self) -> None:
        cases: list[Any] = [
            "not an object",
            {"text": 5},
            {"text": "   "},
            {"text": "事实", "weight": "heavy"},
            {"text": "事实", "weight": True},
            {"text": "事实", "id": 7},
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                payload = self._valid_payload()
                payload["facts"] = [entry]
                with self.assertRaises(MemoryStoreError):
                    self.memory.import_dict(payload)

    def test_episode_entries_are_checked(self) -> None:
        cases: list[Any] = [
            "not an object",
            {"question": 5},
            {"question": "问题", "topics": "极限"},
            {"question": "问题", "summary": 3},
            {"question": "问题", "ts": 3},
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                payload = self._valid_payload()
                payload["episodes"] = [entry]
                with self.assertRaises(MemoryStoreError):
                    self.memory.import_dict(payload)

    def test_topic_entries_are_checked(self) -> None:
        cases: list[Any] = [
            "not an object",
            {"seen": 1},
            {"name": "   ", "seen": 1},
            {"name": 7, "seen": 1},
            _topic("极限", seen="3"),  # type: ignore[arg-type]
            _topic("极限", seen=-1),
            _topic("极限", ease="high"),  # type: ignore[arg-type]
            _topic("极限", due=3),  # type: ignore[arg-type]
            _topic("极限", correct=True),  # type: ignore[arg-type]
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                payload = self._valid_payload()
                payload["topics"] = [entry]
                with self.assertRaises(MemoryStoreError):
                    self.memory.import_dict(payload)

    def test_error_names_the_offending_entry(self) -> None:
        payload = self._valid_payload()
        payload["facts"][0]["weight"] = "heavy"
        with self.assertRaises(MemoryStoreError) as caught:
            self.memory.import_dict(payload)
        self.assertIn("facts[0]", str(caught.exception))

    def test_invalid_payload_leaves_memory_untouched(self) -> None:
        self.memory.set_profile(name="小明", major="数学")
        self.memory.add_fact("保留这条记忆")
        self.memory.record_episode("保留这个问题")
        self.memory.grade_topic("极限", correct=True)
        before = self.memory.export_dict()

        payload = self.memory.export_dict()
        payload["topics"][0]["seen"] = "很多"

        with self.assertRaises(MemoryStoreError):
            self.memory.import_dict(payload)
        self.assertEqual(self.memory.export_dict(), before)

        with self.assertRaises(MemoryStoreError):
            self.memory.import_dict(payload, merge=False)
        self.assertEqual(self.memory.export_dict(), before)


class TestImportLimits(TestMemoryBase):
    def test_facts_are_trimmed_to_max_facts(self) -> None:
        facts = [
            _fact(f"事实{i}", id=f"f{i:04d}", ts=f"2024-01-01T{i // 60:02d}:{i % 60:02d}:00")
            for i in range(MAX_FACTS + 5)
        ]
        payload = _snapshot(facts=facts)
        target = Memory(self.root / "target")
        counts = target.import_dict(payload)
        self.assertEqual(counts["facts_added"], MAX_FACTS + 5)
        self.assertEqual(len(target.facts), MAX_FACTS)
        self.assertEqual(target.facts[0].text, "事实5")  # weakest/oldest five dropped

    def test_episodes_are_trimmed_to_max_episodes(self) -> None:
        episodes = [
            _episode(f"问题{i}", id=f"e{i:04d}", ts=f"2024-01-01T{i // 60:02d}:{i % 60:02d}:00")
            for i in range(MAX_EPISODES + 5)
        ]
        payload = _snapshot(episodes=episodes)
        target = Memory(self.root / "target")
        counts = target.import_dict(payload)
        self.assertEqual(counts["episodes_added"], MAX_EPISODES + 5)
        self.assertEqual(len(target.episodes), MAX_EPISODES)
        self.assertEqual(target.episodes[0].question, "问题5")  # newest 500 kept
        self.assertEqual(target.episodes[-1].question, f"问题{MAX_EPISODES + 4}")


class TestPayloadFileHelpers(TestMemoryBase):
    def test_save_and_load_payload_round_trip(self) -> None:
        path = self.root / "memory-backup.json"
        payload = _snapshot(profile={"name": "小明"}, facts=[_fact("事实", id="f0001")])
        save_payload(path, payload)
        self.assertEqual(load_payload(path), payload)
        self.assertTrue(path.read_text("utf-8").endswith("\n"))

    def test_load_payload_rejects_invalid_json(self) -> None:
        path = self.root / "broken.json"
        path.write_text("{not json", "utf-8")
        with self.assertRaises(MemoryStoreError):
            load_payload(path)

    def test_load_payload_rejects_non_objects(self) -> None:
        path = self.root / "list.json"
        path.write_text("[]", "utf-8")
        with self.assertRaises(MemoryStoreError):
            load_payload(path)

    def test_load_payload_rejects_missing_files(self) -> None:
        with self.assertRaises(MemoryStoreError):
            load_payload(self.root / "missing.json")

    def test_save_payload_rejects_unserializable_data(self) -> None:
        path = self.root / "bad.json"
        with self.assertRaises(MemoryStoreError):
            save_payload(path, {"version": 1, "facts": [{"text": object()}]})
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
