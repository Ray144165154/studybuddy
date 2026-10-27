"""Long-term memory: facts, recall, topics and the spaced-repetition schedule."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studybuddy import Memory


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


if __name__ == "__main__":
    unittest.main()
