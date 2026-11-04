"""Deterministic day-by-day exam revision plans.

Pure logic: this module never touches the filesystem, the network, or the clock.
The same library, filters, day count and start date always produce the same plan,
which is what makes the plan easy to test and easy to print.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from .errors import StudyError
from .types import Book, Chunk

if TYPE_CHECKING:
    from .memory import Memory
    from .store import Library

__all__ = ["PlanDay", "StudyPlan", "build_plan"]

_MAX_DAYS = 60
_DUE_TOPIC_LIMIT = 10
_PLACEHOLDER_SECTION = "未分节"

_READ_FOCUS = (
    "先读定义，再做 3 道例题",
    "边读边整理一页概念/公式卡片",
    "合上书用自己的话复述本节要点",
    "挑 2 道课后题限时完成，再对答案",
    "对照目录画出本节的知识结构",
)
_REVIEW_FOCUS = "只看错题：回看错题本和薄弱知识点，不再开新内容"
_MOCK_FOCUS = "整卷模拟 + 错题回顾：按考试时长限时完成一套真题，对答案后整理错题"
_EMPTY_FOCUS = "机动复习：整理错题与笔记，补齐前面落下的内容"

# Chinese chapter numerals, enough for any textbook a one-semester plan covers.
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUMBER = r"[零〇一二三四五六七八九十百千两\d]+"

_CN_CHAPTER_HEADING = re.compile(rf"^第\s*({_CN_NUMBER})\s*章")
_SECTION_NUMBER = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})")
_CHAPTER_RANGE_SPEC = re.compile(r"^(\d{1,2})\s*[-–—~]\s*(\d{1,2})$")
_DOTTED_SECTION_SPEC = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,2})$")
_PLAIN_CHAPTER_SPEC = re.compile(r"^(\d{1,2})$")
_SPEC_SEPARATORS = re.compile(r"[,，、;；]")


@dataclass(slots=True)
class PlanDay:
    """One day of the plan.

    ``date`` is an ISO date only when the caller supplied ``start``; otherwise it
    stays empty so a plan never silently depends on today's date.
    """

    day: int
    date: str
    title: str
    sections: list[str]
    chunks: int
    pages: tuple[int, int] | None
    review: list[str]
    focus: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "date": self.date,
            "title": self.title,
            "sections": list(self.sections),
            "chunks": self.chunks,
            "pages": list(self.pages) if self.pages is not None else None,
            "review": list(self.review),
            "focus": self.focus,
        }


@dataclass(slots=True)
class StudyPlan:
    """A complete revision plan, renderable as plain Chinese text or as JSON."""

    days: list[PlanDay]
    books: list[str]
    range_label: str
    total_chunks: int

    def render(self) -> str:
        """A stable, directly printable Chinese schedule."""
        lines = [
            f"期末复习计划 · 共 {len(self.days)} 天 · {self.range_label}",
            f"书籍: {'、'.join(f'《{title}》' for title in self.books)}",
            f"片段总数: {self.total_chunks}",
        ]
        for day in self.days:
            lines.append("")
            heading = day.title if not day.date else f"{day.title}（{day.date}）"
            lines.append(heading)
            if day.sections:
                lines.append(f"  小节: {'；'.join(day.sections)}")
            progress = f"  进度: {day.chunks} 个片段"
            if day.pages is not None:
                progress += f" · 第 {day.pages[0]}-{day.pages[1]} 页"
            lines.append(progress)
            if day.review:
                lines.append(f"  复习: {'、'.join(day.review)}")
            lines.append(f"  重点: {day.focus}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": [day.to_dict() for day in self.days],
            "books": list(self.books),
            "range_label": self.range_label,
            "total_chunks": self.total_chunks,
        }


@dataclass(frozen=True, slots=True)
class _Selector:
    """One parsed ``chapters`` token.

    A chapter selector matches ``3`` and every ``3.x`` heading; a section selector
    matches only the ``3.1`` branch. Keeping them separate is what makes
    ``chapters="3"`` and ``chapters="3.1"`` behave differently.
    """

    label: str
    chapters: tuple[int, ...] = ()
    section: tuple[int, ...] = ()


def build_plan(
    library: Library,
    *,
    book: str | None = None,
    chapters: str | None = None,
    days: int = 7,
    start: date | None = None,
    memory: Memory | None = None,
) -> StudyPlan:
    """Build a deterministic revision plan from an indexed library.

    ``book`` follows :meth:`Library.resolve_books` (id, exact title, or unique
    substring). ``chapters`` accepts ``"3"``, ``"3.1"``, ``"3-5"``, ``"第三章"``
    and comma-separated combinations. ``days`` must be 1..60. When ``start`` is
    omitted every ``PlanDay.date`` stays empty. ``memory`` is read (never written)
    for due topics, which rotate across days.
    """
    if not isinstance(days, int) or not 1 <= days <= _MAX_DAYS:
        raise StudyError(f"复习天数必须是 1 到 {_MAX_DAYS} 之间的整数（收到 {days!r}）")

    references = [book] if book is not None else None
    selected = library.resolve_books(references)
    if not selected:
        raise StudyError("书库是空的 -- 先用 `studybuddy add` 导入一本教材")

    selectors, range_label = _parse_chapters(chapters)
    content = _content_chunks(library, selected)
    if not content:
        titles = "、".join(f"《{item.title}》" for item in selected)
        raise StudyError(f"{titles} 没有可复习的内容片段，请先导入教材或换一本书")

    sections: list[str] = []
    seen_sections: set[str] = set()
    for chunk in content:
        name = chunk.section.strip()
        if name and name not in seen_sections:
            seen_sections.add(name)
            sections.append(name)

    if selectors:
        matched = [name for name in sections if any(_matches(selector, name) for selector in selectors)]
        if not matched:
            hint = _available_hint(sections)
            raise StudyError(
                f"章节 {chapters!r} 没有匹配到任何小节；可选章节: {hint}。"
                '支持 "3"（整章）、"3.1"（单节）、"3-5"（范围）、'
                '"第三章"，多个用逗号分隔'
            )
        allowed = set(matched)
        chunks = [chunk for chunk in content if chunk.section.strip() in allowed]
    else:
        chunks = content

    total_chunks = len(chunks)
    base, extra = divmod(total_chunks, days)
    due_topics = _due_names(memory)
    book_titles = {item.id: item.title for item in selected}
    multi_book = len(selected) > 1

    plan_days: list[PlanDay] = []
    offset = 0
    for index in range(days):
        size = base + (1 if index < extra else 0)
        day_chunks = chunks[offset : offset + size]
        offset += size
        day_number = index + 1

        day_sections: list[str] = []
        seen_in_day: set[str] = set()
        for chunk in day_chunks:
            name = chunk.section.strip() or _PLACEHOLDER_SECTION
            if name not in seen_in_day:
                seen_in_day.add(name)
                day_sections.append(name)

        page_numbers = [chunk.page for chunk in day_chunks if chunk.page > 0]
        pages = (min(page_numbers), max(page_numbers)) if page_numbers else None
        chapter_label = _day_chapter_label(day_chunks, book_titles, multi_book=multi_book)
        review = [due_topics[index % len(due_topics)]] if due_topics else []

        plan_days.append(
            PlanDay(
                day=day_number,
                date=(start + timedelta(days=index)).isoformat() if start is not None else "",
                title=f"第 {day_number} 天 · {chapter_label}",
                sections=day_sections,
                chunks=len(day_chunks),
                pages=pages,
                review=review,
                focus=_focus_for(day_number, days, has_chunks=bool(day_chunks)),
            )
        )

    return StudyPlan(
        days=plan_days,
        books=[item.title for item in selected],
        range_label=range_label,
        total_chunks=total_chunks,
    )


def _focus_for(day_number: int, days: int, *, has_chunks: bool) -> str:
    """The day's one-line emphasis. Deterministic: it only looks at the plan shape."""
    if day_number == days:
        return _MOCK_FOCUS
    if day_number == days - 1:
        return _REVIEW_FOCUS
    if not has_chunks:
        return _EMPTY_FOCUS
    return _READ_FOCUS[(day_number - 1) % len(_READ_FOCUS)]


def _content_chunks(library: Library, books: Sequence[Book]) -> list[Chunk]:
    """Every non-navigation chunk of ``books``, in library (= book) order."""
    return [chunk for item in books for chunk in library.chunks_of(item.id) if not chunk.nav]


def _due_names(memory: Memory | None) -> list[str]:
    if memory is None:
        return []
    names: list[str] = []
    for topic in memory.due_topics(limit=_DUE_TOPIC_LIMIT):
        name = topic.name.strip()
        if name and name not in names:
            names.append(name)
    return names


def _day_chapter_label(chunks: Sequence[Chunk], book_titles: dict[str, str], *, multi_book: bool) -> str:
    if not chunks:
        return "机动复习"
    if not multi_book:
        return _chapter_span(chunks)

    groups: dict[str, list[Chunk]] = {}
    order: list[str] = []
    for chunk in chunks:
        title = book_titles.get(chunk.book_id, chunk.book_title)
        if title not in groups:
            groups[title] = []
            order.append(title)
        groups[title].append(chunk)
    return "；".join(f"《{title}》{_chapter_span(groups[title])}" for title in order)


def _chapter_span(chunks: Sequence[Chunk]) -> str:
    entries: list[tuple[int | str, str]] = []
    seen: set[int | str] = set()
    for chunk in chunks:
        number, label = _chapter_of(chunk.section)
        key: int | str = number if number is not None else label
        if key in seen:
            continue
        seen.add(key)
        entries.append((key, label))

    named = [entry for entry in entries if entry[1] != _PLACEHOLDER_SECTION]
    if named:
        entries = named
    if not entries:
        return _PLACEHOLDER_SECTION

    if len(entries) > 1:
        numeric = [key for key, _label in entries if isinstance(key, int)]
        if len(numeric) == len(entries):
            first = numeric[0]
            if numeric == list(range(first, first + len(numeric))):
                return f"第 {first}-{numeric[-1]} 章"
    return "、".join(label for _key, label in entries)


def _chapter_of(section: str) -> tuple[int | None, str]:
    """Return ``(chapter number, human label)`` for a section heading."""
    stripped = section.strip()
    if not stripped:
        return None, _PLACEHOLDER_SECTION

    heading = _CN_CHAPTER_HEADING.match(stripped)
    if heading is not None:
        number = _cn_to_int(heading.group(1))
        if number is not None:
            return number, stripped

    numbers = _section_numbers(stripped)
    if numbers is not None:
        return numbers[0], f"第 {numbers[0]} 章"
    return None, stripped


def _section_numbers(section: str) -> tuple[int, ...] | None:
    match = _SECTION_NUMBER.match(section.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _parse_chapters(chapters: str | None) -> tuple[list[_Selector], str]:
    if chapters is None or not chapters.strip():
        return [], "全书"

    selectors: list[_Selector] = []
    labels: list[str] = []
    for raw in _SPEC_SEPARATORS.split(chapters):
        token = raw.strip()
        if not token:
            continue
        selector = _selector_for(token)
        selectors.append(selector)
        if selector.label not in labels:
            labels.append(selector.label)
    if not selectors:
        return [], "全书"
    return selectors, "、".join(labels)


def _selector_for(token: str) -> _Selector:
    match = _CHAPTER_RANGE_SPEC.fullmatch(token)
    if match is not None:
        first, last = int(match.group(1)), int(match.group(2))
        if first < 1 or last < 1:
            raise StudyError(f"章节编号从 1 开始，收到 {token!r}")
        if first > last:
            raise StudyError(f"章节范围 {token!r} 不合法：起始章节不能大于结束章节")
        label = f"第 {first} 章" if first == last else f"第 {first}-{last} 章"
        return _Selector(label, chapters=tuple(range(first, last + 1)))

    match = _DOTTED_SECTION_SPEC.fullmatch(token)
    if match is not None:
        numbers = tuple(int(part) for part in match.group(1).split("."))
        label = "§" + ".".join(str(number) for number in numbers)
        return _Selector(label, section=numbers)

    match = _PLAIN_CHAPTER_SPEC.fullmatch(token)
    if match is not None:
        number = int(match.group(1))
        if number < 1:
            raise StudyError(f"章节编号从 1 开始，收到 {token!r}")
        return _Selector(f"第 {number} 章", chapters=(number,))

    match = _CN_CHAPTER_HEADING.match(token)
    if match is not None:
        chinese_number = _cn_to_int(match.group(1))
        if chinese_number is not None and 1 <= chinese_number <= 99:
            return _Selector(f"第 {chinese_number} 章", chapters=(chinese_number,))

    raise StudyError(
        f'无法理解的章节写法 {token!r}：支持 "3"（整章）、"3.1"（单节）、'
        '"3-5"（章节范围）、"第三章"，多个用逗号分隔'
    )


def _matches(selector: _Selector, section: str) -> bool:
    if selector.section:
        numbers = _section_numbers(section)
        return numbers is not None and numbers[: len(selector.section)] == selector.section
    number, _label = _chapter_of(section)
    return number is not None and number in selector.chapters


def _available_hint(sections: Sequence[str]) -> str:
    seen: dict[int | str, str] = {}
    for section in sections:
        number, label = _chapter_of(section)
        if label == _PLACEHOLDER_SECTION:
            continue
        key: int | str = number if number is not None else label
        if key in seen:
            continue
        seen[key] = _hint_label(number, label)
    labels = list(seen.values())
    if not labels:
        return "（无可用章节）"
    if len(labels) > 10:
        labels = [*labels[:10], "…"]
    return "、".join(labels)


def _hint_label(number: int | None, label: str) -> str:
    """Prefer the user's numeric vocabulary: ``第 1 章 函数与极限``."""
    if number is None:
        return label
    heading = _CN_CHAPTER_HEADING.match(label)
    remainder = label[heading.end() :].strip() if heading is not None else ""
    return f"第 {number} 章" + (f" {remainder}" if remainder else "")


def _cn_to_int(text: str) -> int | None:
    value = text.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)

    total = 0
    current = 0
    for char in value:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            total += (current or 1) * _CN_UNITS[char]
            current = 0
        else:
            return None
    return total + current
