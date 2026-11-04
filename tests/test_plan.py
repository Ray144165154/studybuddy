"""The exam study plan: deterministic, balanced, and honest about filters."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from studybuddy.errors import StudyError
from studybuddy.plan import build_plan
from studybuddy.store import Library
from studybuddy.types import Book, TopicState

MATH_TITLE = "高等数学"
MATH_PAGES = [
    (
        1,
        "第一章 函数与极限\n"
        "本章讨论函数、极限与连续。\n"
        "1.1 函数的概念\n"
        "函数是描述变量之间依赖关系的工具。\n"
        "1.2 极限\n"
        "极限描述数列和函数的趋向。",
    ),
    (
        2,
        "第二章 导数与微分\n"
        "本章从变化率引出导数。\n"
        "2.1 导数的概念\n"
        "导数是函数的变化率。\n"
        "2.2 求导法则\n"
        "掌握乘积法则、商法则与链式法则。",
    ),
    (
        3,
        "第三章 积分\n"
        "本章讨论积分的概念与应用。\n"
        "3.1 不定积分\n"
        "不定积分是求导的逆运算。\n"
        "3.2 定积分\n"
        "定积分可以用来计算面积。",
    ),
]

LINEAR_TITLE = "线性代数"
LINEAR_PAGES = [
    (
        1,
        "第一章 行列式\n行列式给出方阵的一个数值。\n1.1 行列式的定义\n二阶行列式按对角线展开。",
    ),
    (
        2,
        "第二章 矩阵\n矩阵是数的矩形阵列。\n2.1 矩阵的运算\n矩阵加法按元素进行。",
    ),
]


class FakeMemory:
    """Only the read side of ``Memory`` that the planner is allowed to touch."""

    def __init__(self, *names: str) -> None:
        self.names = list(names)
        self.calls: list[int] = []

    def due_topics(self, *, limit: int = 10) -> list[TopicState]:
        self.calls.append(limit)
        return [TopicState(name=name) for name in self.names[:limit]]


def sections_of(library: Library, title: str) -> list[str]:
    book = library.get_book(title)
    return [chunk.section for chunk in library.chunks_of(book.id) if not chunk.nav]


class PlanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "store"

    def make_library(self, *books: tuple[str, list[tuple[int, str]]]) -> Library:
        library = Library(self.root)
        for title, pages in books:
            library.add_document(title=title, pages=pages)
        return library

    def math_library(self) -> Library:
        return self.make_library((MATH_TITLE, MATH_PAGES))


class TestPlanShape(PlanTestCase):
    def test_single_book_covers_every_chunk_in_book_order(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=4)

        self.assertEqual(plan.books, [MATH_TITLE])
        self.assertEqual(plan.range_label, "全书")
        self.assertEqual(plan.total_chunks, 9)
        self.assertEqual([day.day for day in plan.days], [1, 2, 3, 4])
        self.assertEqual([day.date for day in plan.days], ["", "", "", ""])
        self.assertEqual([day.chunks for day in plan.days], [3, 2, 2, 2])
        self.assertEqual(sum(day.chunks for day in plan.days), plan.total_chunks)

        flattened = [section for day in plan.days for section in day.sections]
        self.assertEqual(flattened, sections_of(library, MATH_TITLE))

    def test_chunk_counts_are_balanced(self) -> None:
        library = self.math_library()
        for days in (1, 2, 4, 5):
            sizes = [day.chunks for day in build_plan(library, days=days).days]
            self.assertEqual(sum(sizes), 9)
            self.assertLessEqual(max(sizes) - min(sizes), 1, f"days={days}: {sizes}")

    def test_days_are_contiguous_blocks(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=3)
        self.assertEqual([day.chunks for day in plan.days], [3, 3, 3])
        day_sections = [day.sections for day in plan.days]
        self.assertEqual(day_sections[0], ["第一章 函数与极限", "1.1 函数的概念", "1.2 极限"])
        self.assertEqual(day_sections[2], ["第三章 积分", "3.1 不定积分", "3.2 定积分"])

    def test_sections_are_deduplicated_inside_a_day(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=1)
        self.assertEqual(len(plan.days[0].sections), len(set(plan.days[0].sections)))
        self.assertEqual(plan.days[0].sections, sections_of(library, MATH_TITLE))

    def test_page_ranges_cover_the_days_chunks(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=3)
        self.assertEqual(plan.days[0].pages, (1, 1))
        self.assertEqual(plan.days[1].pages, (2, 2))
        self.assertEqual(plan.days[2].pages, (3, 3))
        self.assertIsInstance(plan.days[0].pages, tuple)

    def test_day_titles_and_focuses(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=3)
        self.assertEqual(plan.days[0].title, "第 1 天 · 第一章 函数与极限")
        self.assertEqual(plan.days[2].title, "第 3 天 · 第三章 积分")
        self.assertIn("模拟", plan.days[-1].focus)
        self.assertIn("错题", plan.days[-1].focus)
        self.assertIn("只看错题", plan.days[-2].focus)
        self.assertTrue(plan.days[0].focus)

    def test_single_day_plan_is_a_mock_exam(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=1)
        self.assertEqual(len(plan.days), 1)
        self.assertIn("模拟", plan.days[0].focus)
        self.assertEqual(plan.total_chunks, 9)

    def test_start_date_is_iso_and_sequential(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=3, start=date(2026, 1, 10))
        self.assertEqual([day.date for day in plan.days], ["2026-01-10", "2026-01-11", "2026-01-12"])

    def test_plan_is_deterministic(self) -> None:
        library = self.math_library()
        first = build_plan(library, book="高等", chapters="1-2", days=3, start=date(2026, 2, 1))
        second = build_plan(library, book="高等", chapters="1-2", days=3, start=date(2026, 2, 1))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.render(), second.render())


class TestSelection(PlanTestCase):
    def test_book_by_substring(self) -> None:
        library = self.make_library((MATH_TITLE, MATH_PAGES), (LINEAR_TITLE, LINEAR_PAGES))
        plan = build_plan(library, book="高等", days=2)
        self.assertEqual(plan.books, [MATH_TITLE])
        self.assertEqual(plan.total_chunks, 9)

    def test_multi_book_follows_library_order(self) -> None:
        library = self.make_library((MATH_TITLE, MATH_PAGES), (LINEAR_TITLE, LINEAR_PAGES))
        plan = build_plan(library, days=4)
        self.assertEqual(plan.books, [MATH_TITLE, LINEAR_TITLE])
        self.assertEqual(plan.total_chunks, 13)
        expected = sections_of(library, MATH_TITLE) + sections_of(library, LINEAR_TITLE)
        flattened = [section for day in plan.days for section in day.sections]
        self.assertEqual(flattened, expected)

    def test_numeric_chapter_matches_chapter_and_its_sections(self) -> None:
        library = self.math_library()
        plan = build_plan(library, chapters="2", days=2)
        self.assertEqual(plan.total_chunks, 3)
        self.assertEqual(plan.range_label, "第 2 章")
        self.assertEqual(
            [section for day in plan.days for section in day.sections],
            ["第二章 导数与微分", "2.1 导数的概念", "2.2 求导法则"],
        )

    def test_dotted_section_matches_only_that_section(self) -> None:
        library = self.math_library()
        chapter = build_plan(library, chapters="2", days=1)
        section = build_plan(library, chapters="2.1", days=1)
        self.assertEqual(chapter.total_chunks, 3)
        self.assertEqual(section.total_chunks, 1)
        self.assertEqual(section.range_label, "§2.1")
        self.assertEqual(section.days[0].sections, ["2.1 导数的概念"])

    def test_chapter_range(self) -> None:
        library = self.math_library()
        plan = build_plan(library, chapters="1-2", days=2)
        self.assertEqual(plan.total_chunks, 6)
        self.assertEqual(plan.range_label, "第 1-2 章")
        self.assertNotIn("第三章 积分", [section for day in plan.days for section in day.sections])

    def test_chinese_chapter_number(self) -> None:
        library = self.math_library()
        plan = build_plan(library, chapters="第三章", days=1)
        self.assertEqual(plan.total_chunks, 3)
        self.assertEqual(plan.range_label, "第 3 章")
        self.assertEqual(plan.days[0].sections, ["第三章 积分", "3.1 不定积分", "3.2 定积分"])

    def test_comma_separated_mixed_selectors(self) -> None:
        library = self.math_library()
        plan = build_plan(library, chapters="1,3.2", days=2)
        self.assertEqual(plan.total_chunks, 4)
        self.assertEqual(plan.range_label, "第 1 章、§3.2")
        flattened = [section for day in plan.days for section in day.sections]
        self.assertNotIn("2.1 导数的概念", flattened)
        self.assertIn("3.2 定积分", flattened)

    def test_no_matching_chapter_mentions_available_ones(self) -> None:
        library = self.math_library()
        with self.assertRaises(StudyError) as caught:
            build_plan(library, chapters="9", days=2)
        message = str(caught.exception)
        self.assertIn("可选章节", message)
        self.assertIn("第 1 章", message)
        self.assertIn("支持", message)

    def test_malformed_chapter_spec_is_actionable(self) -> None:
        library = self.math_library()
        with self.assertRaises(StudyError) as caught:
            build_plan(library, chapters="abc", days=2)
        self.assertIn("支持", str(caught.exception))

    def test_reversed_range_is_rejected(self) -> None:
        library = self.math_library()
        with self.assertRaises(StudyError):
            build_plan(library, chapters="5-3", days=2)


class TestEdgeCases(PlanTestCase):
    def test_invalid_days(self) -> None:
        library = self.math_library()
        for days in (0, -1, 61, 100):
            with self.assertRaises(StudyError, msg=f"days={days}"):
                build_plan(library, days=days)

    def test_days_bounds_are_accepted(self) -> None:
        library = self.math_library()
        self.assertEqual(len(build_plan(library, days=1).days), 1)
        self.assertEqual(len(build_plan(library, days=60).days), 60)

    def test_more_days_than_chunks_leaves_empty_tail_days(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=12)
        self.assertEqual(len(plan.days), 12)
        self.assertEqual([day.chunks for day in plan.days], [1] * 9 + [0] * 3)
        for day in plan.days[9:]:
            self.assertEqual(day.sections, [])
            self.assertIsNone(day.pages)
        self.assertIn("模拟", plan.days[-1].focus)

    def test_empty_library_is_actionable(self) -> None:
        library = Library(self.root)
        with self.assertRaises(StudyError) as caught:
            build_plan(library, days=3)
        self.assertIn("书库是空的", str(caught.exception))

    def test_book_without_chunks_is_actionable(self) -> None:
        library = self.math_library()
        library.books = [Book(id="empty-book", title="空书")]
        library.chunks = []
        with self.assertRaises(StudyError) as caught:
            build_plan(library, book="空书", days=3)
        self.assertIn("可复习", str(caught.exception))


class TestMemoryAndOutput(PlanTestCase):
    def test_due_topics_rotate_across_days(self) -> None:
        library = self.math_library()
        memory = FakeMemory("导数", "积分")
        plan = build_plan(library, days=4, memory=memory)
        self.assertEqual([day.review for day in plan.days], [["导数"], ["积分"], ["导数"], ["积分"]])
        self.assertTrue(memory.calls)

    def test_no_memory_means_no_review(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=2)
        self.assertEqual([day.review for day in plan.days], [[], []])

    def test_to_dict_is_json_ready(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=3, start=date(2026, 3, 2))
        data = plan.to_dict()
        self.assertEqual(set(data), {"days", "books", "range_label", "total_chunks"})
        self.assertEqual(data["books"], [MATH_TITLE])
        self.assertEqual(len(data["days"]), 3)
        day = data["days"][0]
        self.assertEqual(set(day), {"day", "date", "title", "sections", "chunks", "pages", "review", "focus"})
        self.assertEqual(day["pages"], [1, 1])
        self.assertEqual(day["date"], "2026-03-02")
        self.assertEqual(json.loads(json.dumps(data, ensure_ascii=False)), data)

    def test_render_is_human_readable(self) -> None:
        library = self.math_library()
        plan = build_plan(library, days=3, memory=FakeMemory("导数"))
        text = plan.render()
        self.assertIn("期末复习计划", text)
        self.assertIn("《高等数学》", text)
        self.assertIn("第 1 天 · 第一章 函数与极限", text)
        self.assertIn("小节:", text)
        self.assertIn("进度:", text)
        self.assertIn("复习: 导数", text)
        self.assertIn("重点:", text)
        self.assertIn("模拟", text)
