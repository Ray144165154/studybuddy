"""The orchestrator: citations, memory writes, quiz/summary/compare/review."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from studybuddy import Book, Chunk, Library, Memory, StudyBuddy
from studybuddy.errors import StudyError
from studybuddy.study import normalize_math_text, split_query
from support import ScriptedLLM, make_textbook, says

MEMORY_JSON = json.dumps(
    {
        "topics": ["导数"],
        "facts": [{"text": "对导数的定义不熟", "kind": "weakness", "topic": "导数"}],
        "summary": "询问了导数的定义",
    },
    ensure_ascii=False,
)

QUIZ_JSON = json.dumps(
    {
        "topic": "导数",
        "questions": [
            {"question": "导数的定义是什么？", "answer": "瞬时变化率", "hint": "极限", "difficulty": "easy"},
            {
                "question": "链式法则怎么用？",
                "answer": "外层导数乘内层",
                "hint": "复合函数",
                "difficulty": "medium",
            },
        ],
    },
    ensure_ascii=False,
)


def user_prompt(llm: ScriptedLLM, call_index: int = 0) -> str:
    """The user message of one specific model call (not the memory extraction one)."""
    for message in llm.calls[call_index]:
        if message.role == "user":
            return message.content or ""
    return ""


class StudyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "data"
        # Built by hand rather than from a PDF: these fixtures need Chinese text, and
        # the dependency-free PDF builder can only embed Latin-1 fonts.
        self.library = Library(self.root)
        self.library.books = [
            Book(id="math-1", title="高等数学", course="高等数学", pages=2, chunks=2),
            Book(id="la-1", title="线性代数", course="线性代数", pages=1, chunks=1),
        ]
        self.library.chunks = [
            Chunk(
                id="math-1:p12:0",
                book_id="math-1",
                book_title="高等数学",
                page=12,
                section="3.1 导数的概念",
                course="高等数学",
                text="导数定义为函数增量与自变量增量之比的极限，记作 f'(x)，它刻画瞬时变化率。",
            ),
            Chunk(
                id="math-1:p40:0",
                book_id="math-1",
                book_title="高等数学",
                page=40,
                section="4.2 中值定理",
                course="高等数学",
                text="拉格朗日中值定理是柯西中值定理的特例，罗尔定理又是拉格朗日中值定理的特例。",
            ),
            Chunk(
                id="la-1:p5:0",
                book_id="la-1",
                book_title="线性代数",
                page=5,
                section="1.1 矩阵",
                course="线性代数",
                text="矩阵乘法不满足交换律，但满足结合律与分配律。",
            ),
        ]
        self.library.save()
        self.book = self.library.books[0]
        self.memory = Memory(self.root)

    def buddy(self, *responses) -> StudyBuddy:
        return StudyBuddy(
            self.root,
            model="test-model",
            llm=ScriptedLLM(*responses),
            library=self.library,
            memory=self.memory,
        )


class TestQuerySplit(unittest.TestCase):
    def test_two_topics_are_split(self) -> None:
        self.assertEqual(
            split_query("条件概率和事件的独立性有什么关系"),
            ["条件概率", "事件的独立性"],
        )

    def test_interrogative_tail_does_not_produce_a_fragment(self) -> None:
        # "条件和结论" are two aspects of one topic, not two topics.
        self.assertEqual(split_query("中心极限定理的条件和结论是什么？"), [])

    def test_short_question_is_not_split(self) -> None:
        self.assertEqual(split_query("熵与信息"), [])

    def test_single_topic_is_not_split(self) -> None:
        self.assertEqual(split_query("矩阵乘法为什么不满足交换律"), [])


class TestMathText(unittest.TestCase):
    """Decorative math letters render as tofu boxes in many fonts."""

    def test_astral_math_letters_become_ascii(self) -> None:
        self.assertEqual(normalize_math_text("在 𝒬 上，𝓕(x)"), "在 Q 上，F(x)")

    def test_plain_text_is_untouched(self) -> None:
        text = "条件概率 P(A|B) = P(AB)/P(B)"
        self.assertEqual(normalize_math_text(text), text)

    def test_superscripts_and_letterlike_symbols_survive(self) -> None:
        text = "f⁻¹(B) ⊂ ℝ^n，σ(f) 可测"
        self.assertEqual(normalize_math_text(text), text)

    def test_empty(self) -> None:
        self.assertEqual(normalize_math_text(""), "")


class TestAsk(StudyTestCase):
    def test_answer_has_numbered_sources(self) -> None:
        buddy = self.buddy(says("导数是瞬时变化率 [1]。"), says(MEMORY_JSON))
        answer = buddy.ask("什么是导数？")

        self.assertIn("[1]", answer.text)
        self.assertTrue(answer.sources)
        self.assertEqual(answer.sources[0].index, 1)
        self.assertEqual(answer.sources[0].chunk.book_title, "高等数学")

    def test_with_citations_lists_pages(self) -> None:
        buddy = self.buddy(says("答案 [1]"), says(MEMORY_JSON))
        rendered = buddy.ask("什么是导数？").with_citations()
        self.assertIn("来源", rendered)
        self.assertIn("《高等数学》", rendered)
        self.assertIn("第12页", rendered)

    def test_prompt_contains_sources_and_question(self) -> None:
        llm = ScriptedLLM(says("答 [1]"), says(MEMORY_JSON))
        buddy = StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory)
        buddy.ask("什么是导数？")
        prompt = user_prompt(llm, 0)
        self.assertIn("[1]", prompt)
        self.assertIn("什么是导数", prompt)
        self.assertIn("教材片段", prompt)

    def test_prompt_warns_about_unreliable_ocr(self) -> None:
        self.library.chunks = [
            Chunk(
                id="m:p1:0",
                book_id="math-1",
                book_title="高等数学",
                page=1,
                section="1.1 导数",
                text="定义：导数是瞬时变化率。",
                formula_ratio=0.5,
            )
        ]
        self.library.save()
        llm = ScriptedLLM(says("答 [1]"), says(MEMORY_JSON))
        StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory).ask("什么是导数？")
        self.assertIn("OCR 可能失真", user_prompt(llm, 0))

    def test_prompt_has_no_warning_for_clean_text(self) -> None:
        llm = ScriptedLLM(says("答 [1]"), says(MEMORY_JSON))
        StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory).ask("什么是导数？")
        self.assertNotIn("OCR 可能失真", user_prompt(llm, 0))

    def test_prompt_includes_profile(self) -> None:
        self.memory.set_profile(major="计算机科学与技术", courses=["高等数学"])
        llm = ScriptedLLM(says("答 [1]"), says(MEMORY_JSON))
        StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory).ask("导数？")
        self.assertIn("计算机科学与技术", user_prompt(llm, 0))

    def test_ask_writes_memory(self) -> None:
        buddy = self.buddy(says("导数是变化率 [1]"), says(MEMORY_JSON))
        buddy.ask("什么是导数？")

        self.assertEqual(len(self.memory.episodes), 1)
        self.assertIn("导数", self.memory.topics)
        self.assertEqual(len(self.memory.facts), 1)
        self.assertEqual(self.memory.facts[0].kind, "weakness")

    def test_no_memory_skips_extraction(self) -> None:
        llm = ScriptedLLM(says("答 [1]"))
        buddy = StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory)
        buddy.ask("什么是导数？", remember=False)
        self.assertEqual(llm.request_count, 1)
        self.assertEqual(self.memory.episodes, [])

    def test_bad_memory_json_does_not_break_answer(self) -> None:
        buddy = self.buddy(says("答 [1]"), says("抱歉我不能输出 JSON"))
        answer = buddy.ask("什么是导数？")
        self.assertEqual(answer.text, "答 [1]")
        self.assertEqual(len(self.memory.episodes), 1)
        self.assertEqual(self.memory.facts, [])

    def test_extraction_failure_is_swallowed(self) -> None:
        class BrokenLLM(ScriptedLLM):
            def complete(self, messages, tools=None):  # type: ignore[no-untyped-def]
                if len(self.calls) == 0:
                    return super().complete(messages, tools)
                raise RuntimeError("upstream down")

        llm = BrokenLLM(says("答 [1]"))
        answer = StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory).ask("导数？")
        self.assertEqual(answer.text, "答 [1]")

    def test_empty_library_raises(self) -> None:
        empty = StudyBuddy(self.tmp / "empty", llm=ScriptedLLM(says("x")), memory=Memory(self.tmp / "empty"))
        with self.assertRaises(StudyError) as ctx:
            empty.ask("导数？")
        self.assertIn("书库是空的", str(ctx.exception))

    def test_empty_question_raises(self) -> None:
        with self.assertRaises(StudyError):
            self.buddy(says("x")).ask("   ")

    def test_memory_is_recalled_on_next_question(self) -> None:
        buddy = self.buddy(says("答 [1]"), says(MEMORY_JSON))
        buddy.ask("什么是导数？")
        buddy.llm.responses.extend([says("答 [1]"), says(MEMORY_JSON)])
        buddy.ask("导数怎么求？")
        self.assertIn("导数", buddy.memory.recall("导数")[0].text)

    def test_session_history_is_included(self) -> None:
        llm = ScriptedLLM(
            says("第一问答案 [1]"), says(MEMORY_JSON), says("第二问答案 [1]"), says(MEMORY_JSON)
        )
        buddy = StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory)
        buddy.ask("第一个问题")
        buddy.ask("第二个问题")
        self.assertIn("本次会话", user_prompt(llm, 2))


class TestCompoundCoverage(StudyTestCase):
    def test_compound_question_retrieves_both_topics(self) -> None:
        self.library.chunks = [
            Chunk(
                id="m:p1:0",
                book_id="math-1",
                book_title="高等数学",
                page=1,
                section="条件概率",
                text="条件概率是在已知事件发生的条件下另一事件发生的概率。",
            ),
            Chunk(
                id="m:p2:0",
                book_id="math-1",
                book_title="高等数学",
                page=2,
                section="独立性",
                text="两个事件相互独立是指联合概率等于概率之积。",
            ),
            Chunk(
                id="m:p3:0",
                book_id="math-1",
                book_title="高等数学",
                page=3,
                section="其他",
                text="矩阵乘法满足结合律。",
            ),
        ]
        self.library.save()
        buddy = self.buddy(says("答 [1][2]"), says(MEMORY_JSON))
        hits, _warning = buddy._retrieve(
            "条件概率和事件的独立性有什么关系",
            books=buddy.library.books,
            top_k=4,
            per_book_cap=3,
            semantic=False,
        )
        ids = [chunk.id for chunk, _score in hits]
        self.assertIn("m:p1:0", ids)
        self.assertIn("m:p2:0", ids)

    def test_section_lead_is_pulled_in_even_if_body_does_not_match(self) -> None:
        self.library.chunks = [
            Chunk(
                id="m:p10:0",
                book_id="math-1",
                book_title="高等数学",
                page=10,
                section="1.6 独立事件类",
                text="独立性的定义：若联合概率等于乘积，则两事件独立。",
            ),
            Chunk(
                id="m:p11:0",
                book_id="math-1",
                book_title="高等数学",
                page=11,
                section="1.6 独立事件类",
                text="习题：证明独立事件类在取余集下仍独立。",
            ),
        ]
        self.library.save()
        buddy = self.buddy(says("答 [1]"), says(MEMORY_JSON))
        hits, _warning = buddy._retrieve(
            "独立性在取余集下还成立吗",
            books=buddy.library.books,
            top_k=3,
            per_book_cap=3,
            semantic=False,
        )
        ids = [chunk.id for chunk, _score in hits]
        self.assertIn("m:p10:0", ids)


REWRITE_JSON = json.dumps(["导数 微分 关系", "求导 定义"], ensure_ascii=False)


class TestQueryRewrite(StudyTestCase):
    def test_rewrite_queries_parses_json(self) -> None:
        buddy = self.buddy(says(REWRITE_JSON))
        self.assertEqual(buddy.rewrite_queries("为什么求导和微分是一回事"), ["导数 微分 关系", "求导 定义"])

    def test_rewrite_tolerates_markdown_fence(self) -> None:
        buddy = self.buddy(says('```json\n["导数 定义"]\n```'))
        self.assertEqual(buddy.rewrite_queries("导数是什么"), ["导数 定义"])

    def test_invalid_rewrite_returns_empty(self) -> None:
        self.assertEqual(self.buddy(says("抱歉，我无法输出")).rewrite_queries("q"), [])

    def test_rewrite_drops_duplicates_and_the_original(self) -> None:
        buddy = self.buddy(says(json.dumps(["原问题", "原问题", "导数 定义"], ensure_ascii=False)))
        self.assertEqual(buddy.rewrite_queries("原问题"), ["导数 定义"])

    def test_rewrite_failure_does_not_break_ask(self) -> None:
        class FlakyLLM(ScriptedLLM):
            def complete(self, messages, tools=None):  # type: ignore[no-untyped-def]
                if len(self.calls) == 0:
                    self.calls.append(list(messages))
                    raise RuntimeError("rewrite endpoint down")
                return super().complete(messages, tools)

        llm = FlakyLLM(says("答 [1]"), says(MEMORY_JSON))
        answer = StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory).ask(
            "什么是导数？", rewrite=True
        )
        self.assertEqual(answer.text, "答 [1]")

    def test_rewritten_query_brings_in_new_evidence(self) -> None:
        self.library.chunks = [
            Chunk(
                id="m:p1:0",
                book_id="math-1",
                book_title="高等数学",
                page=1,
                section="1.1 导数",
                text="导数刻画瞬时变化率。",
            ),
            Chunk(
                id="m:p2:0",
                book_id="math-1",
                book_title="高等数学",
                page=2,
                section="1.2 微分",
                text="微分与导数互为表里，微分是改变量的线性主部。",
            ),
        ]
        self.library.save()
        llm = ScriptedLLM(says(REWRITE_JSON), says("答 [1][2]"), says(MEMORY_JSON))
        buddy = StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory)
        answer = buddy.ask("为什么求导和微分是一回事", rewrite=True)
        pages = {source.chunk.page for source in answer.sources}
        self.assertIn(1, pages)
        self.assertIn(2, pages)
        self.assertEqual(llm.request_count, 3)

    def test_rewrite_is_off_by_default(self) -> None:
        llm = ScriptedLLM(says("答 [1]"), says(MEMORY_JSON))
        StudyBuddy(self.root, llm=llm, library=self.library, memory=self.memory).ask("什么是导数？")
        self.assertEqual(llm.request_count, 2)


class TestQuiz(StudyTestCase):
    def test_quiz_parses_questions(self) -> None:
        quiz = self.buddy(says(QUIZ_JSON)).quiz(topic="导数", count=2)
        self.assertEqual(quiz.topic, "导数")
        self.assertEqual(len(quiz.questions), 2)
        self.assertEqual(quiz.questions[0].answer, "瞬时变化率")
        self.assertEqual(quiz.book, "高等数学")

    def test_quiz_notes_topic(self) -> None:
        self.buddy(says(QUIZ_JSON)).quiz(topic="导数")
        self.assertIn("导数", self.memory.topics)

    def test_quiz_renders_answers(self) -> None:
        quiz = self.buddy(says(QUIZ_JSON)).quiz(topic="导数")
        rendered = quiz.render(show_answers=True)
        self.assertIn("瞬时变化率", rendered)
        self.assertNotIn("瞬时变化率", quiz.render(show_answers=False))

    def test_invalid_quiz_json(self) -> None:
        quiz = self.buddy(says("not json at all")).quiz(topic="导数")
        self.assertEqual(quiz.questions, [])

    def test_quiz_without_material_raises(self) -> None:
        with self.assertRaises(StudyError):
            self.buddy(says(QUIZ_JSON)).quiz(topic="量子纠缠")


class TestSummaryAndCompare(StudyTestCase):
    def test_summary(self) -> None:
        answer = self.buddy(says("## 一句话概括\n导数是变化率 [1]")).summary(book="高等数学")
        self.assertIn("导数", answer.text)
        self.assertTrue(answer.sources)

    def test_summary_unknown_book(self) -> None:
        with self.assertRaises(StudyError):
            self.buddy(says("x")).summary(book="物理")

    def test_compare_merges_and_dedupes(self) -> None:
        result = self.buddy(says("区别如下 [1][2]")).compare("导数", "极限")
        self.assertIn("区别", result.text)
        self.assertTrue(result.sources)
        ids = [source.chunk.id for source in result.sources]
        self.assertEqual(len(ids), len(set(ids)))

    def test_compare_without_matches(self) -> None:
        with self.assertRaises(StudyError):
            self.buddy(says("x")).compare("量子纠缠", "相对论")


class TestReview(StudyTestCase):
    def test_review_returns_due_topics(self) -> None:
        self.memory.note_topic("导数")
        self.memory.topics["导数"].due = "2000-01-01T00:00:00"
        items = self.buddy(says(QUIZ_JSON)).review(limit=3)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].topic, "导数")
        self.assertTrue(items[0].question)

    def test_review_empty_when_nothing_due(self) -> None:
        self.memory.note_topic("导数")
        self.assertEqual(self.buddy(says(QUIZ_JSON)).review(), [])

    def test_grade_updates_schedule(self) -> None:
        self.memory.note_topic("导数")
        state = self.buddy(says("x")).grade("导数", correct=True)
        self.assertEqual(state.correct, 1)


class TestAddBook(StudyTestCase):
    def test_add_book_records_course(self) -> None:
        pdf = make_textbook(self.tmp / "algebra.pdf")
        buddy = self.buddy(says("x"))
        book = buddy.add_book(pdf, course="线性代数", title="线性代数")
        self.assertEqual(book.course, "线性代数")
        self.assertIn("线性代数", self.memory.profile.courses)


if __name__ == "__main__":
    unittest.main()
