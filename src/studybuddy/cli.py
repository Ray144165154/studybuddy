"""Command line interface: ``studybuddy <command>``.

Designed for a student in a terminal: short commands, no flags required for the
common case, and every answer ends with the pages it came from.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .errors import StudyError
from .study import StudyBuddy

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _style(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _supports_color() else text


def _rule(title: str = "") -> None:
    width = 60
    if title:
        print(_style(f"── {title} " + "─" * max(0, width - len(title) - 4), DIM))
    else:
        print(_style("─" * width, DIM))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="studybuddy",
        description="教材驱动的学习助手：上传 PDF 教材，跨书答疑，带页码引用，并记住你的薄弱点。",
    )
    parser.add_argument("--root", default=".studybuddy", help="数据目录，默认 .studybuddy")
    parser.add_argument("-m", "--model", default=os.environ.get("STUDYBUDDY_MODEL", "gpt-4o-mini"))
    parser.add_argument("-b", "--base-url", default=os.environ.get("STUDYBUDDY_BASE_URL"))
    parser.add_argument("-k", "--api-key", default=None)
    parser.add_argument(
        "-e",
        "--embed-model",
        default=os.environ.get("STUDYBUDDY_EMBED_MODEL"),
        help="embedding 模型，例如 text-embedding-3-small / nomic-embed-text（用于混合检索）",
    )
    parser.add_argument(
        "--embed-base-url",
        default=os.environ.get("STUDYBUDDY_EMBED_BASE_URL"),
        help="embedding 端点，默认复用 --base-url",
    )
    parser.add_argument("--version", action="version", version=f"studybuddy {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    add = sub.add_parser("add", help="导入 PDF 教材")
    add.add_argument("pdf", nargs="+", help="一个或多个 PDF 文件")
    add.add_argument("-c", "--course", default="", help="所属课程，例如 高等数学")
    add.add_argument("-t", "--title", default=None, help="书名（默认取 PDF 元数据或文件名）")
    add.add_argument("-q", "--quiet", action="store_true")

    index_cmd = sub.add_parser("index", help="为已导入教材建立向量索引（混合检索）")
    index_cmd.add_argument("--force", action="store_true", help="忽略已有向量，全部重建")

    add_text = sub.add_parser("add-text", help="导入已抽取好的文本（OCR 结果、epub、笔记）")
    add_text.add_argument("file", help='JSONL: {"page": 1, "text": "..."} 每行一页；纯文本则视为单页')
    add_text.add_argument("-t", "--title", required=True)
    add_text.add_argument("-c", "--course", default="")

    books = sub.add_parser("books", help="列出已导入的教材")
    books.add_argument("--json", action="store_true")

    remove = sub.add_parser("remove", help="删除一本教材及其索引")
    remove.add_argument("book")

    ask = sub.add_parser("ask", help="提问（默认跨所有教材检索）")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--book", action="append", default=None, help="限定教材，可重复")
    ask.add_argument("--top-k", type=int, default=6)
    ask.add_argument("--no-memory", action="store_true", help="不读取也不写入记忆")
    ask.add_argument("--no-semantic", action="store_true", help="只用词法检索，不做向量混合")
    ask.add_argument("--quiet", action="store_true", help="只输出答案正文")

    quiz = sub.add_parser("quiz", help="生成自测题")
    quiz.add_argument("--book", default=None)
    quiz.add_argument("--topic", default=None)
    quiz.add_argument("-n", "--count", type=int, default=5)
    quiz.add_argument("-d", "--difficulty", default="medium", choices=["easy", "medium", "hard"])
    quiz.add_argument("-a", "--answers", action="store_true", help="显示答案")

    summary = sub.add_parser("summary", help="生成复习提纲")
    summary.add_argument("--book", required=True)
    summary.add_argument("--chapter", default=None)

    compare = sub.add_parser("compare", help="对比两个易混概念")
    compare.add_argument("concept_a")
    compare.add_argument("concept_b")

    review = sub.add_parser("review", help="按遗忘曲线复习到期知识点")
    review.add_argument("-n", "--limit", type=int, default=3)

    grade = sub.add_parser("grade", help="记录一次自测结果")
    grade.add_argument("topic")
    group = grade.add_mutually_exclusive_group(required=True)
    group.add_argument("--correct", action="store_true")
    group.add_argument("--wrong", action="store_true")

    memory = sub.add_parser("memory", help="查看或清理记忆")
    memory.add_argument("--forget", default=None, help="删除指定记忆编号")
    memory.add_argument("--clear", action="store_true", help="清空全部记忆")

    profile = sub.add_parser("profile", help="设置学生画像")
    profile.add_argument("--name")
    profile.add_argument("--major")
    profile.add_argument("--grade-level", dest="grade_level")
    profile.add_argument("--course", action="append", default=None)
    profile.add_argument("--goal", action="append", default=None)
    profile.add_argument("--pref", action="append", default=None)

    sub.add_parser("stats", help="显示书库与记忆概况")
    return parser


def _warn(messages: list[str]) -> None:
    for message in messages:
        print(_style(f"注意: {message}", YELLOW), file=sys.stderr)


def _print_answer(answer: Any, *, quiet: bool) -> None:
    _warn(getattr(answer, "warnings", []))
    print(answer.text)
    if quiet or not answer.sources:
        return
    print()
    _rule("来源")
    for source in answer.sources:
        print(f"  [{source.index}] {source.citation}")
    if answer.memories:
        print()
        _rule("我记得")
        for memory in answer.memories[:4]:
            print(f"  · {memory}")


def _cmd_add(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    failures = 0
    for pdf in args.pdf:
        path = Path(pdf)
        if not path.exists():
            print(f"找不到文件: {path}", file=sys.stderr)
            failures += 1
            continue

        def progress(message: str, name: str = path.name) -> None:
            if not args.quiet:
                print(_style(f"  [{name}] {message}", DIM))

        try:
            book = buddy.add_book(path, course=args.course, title=args.title, on_progress=progress)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            print(f"  {path.name}: 导入失败 -- {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"✓ {book.title}  {book.pages} 页, {book.chunks} 个片段  id={book.id}")
        if buddy.library.has_embeddings:
            print(_style(f"  已建立向量索引（{buddy.library.embeddings_model}）", DIM))
        elif buddy.embedding_error:
            print(
                _style(
                    f"  向量索引失败（{buddy.embedding_error}），仍可用词法检索；"
                    "稍后可用 studybuddy index 重试",
                    YELLOW,
                ),
                file=sys.stderr,
            )
    return 1 if failures else 0


def _read_pages(path: Path) -> list[tuple[int, str]]:
    """Read a JSONL page dump, falling back to treating the file as one page."""
    text = path.read_text("utf-8", errors="replace")
    pages: list[tuple[int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return [(1, text)]
        if not isinstance(entry, dict) or "text" not in entry:
            return [(1, text)]
        pages.append((int(entry.get("page") or len(pages) + 1), str(entry["text"])))
    return pages


def _cmd_add_text(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    file = Path(args.file)
    if not file.exists():
        print(f"找不到文件: {file}", file=sys.stderr)
        return 1
    try:
        pages = _read_pages(file)
        book = buddy.add_document(
            title=args.title,
            pages=pages,
            course=args.course,
            source=str(file),
            on_progress=lambda message: print(_style(f"  {message}", DIM)),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"导入失败: {exc}", file=sys.stderr)
        return 1
    print(f"✓ {book.title}  {book.pages} 页, {book.chunks} 个片段  id={book.id}")
    if buddy.embedding_error:
        print(_style(f"  向量索引失败（{buddy.embedding_error}）", YELLOW), file=sys.stderr)
    return 0


def _cmd_books(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    books = buddy.library.books
    if not books:
        print("书库是空的。先运行: studybuddy add 你的教材.pdf --course 高等数学")
        return 0
    if args.json:
        import json

        print(json.dumps([book.to_dict() for book in books], ensure_ascii=False, indent=2))
        return 0
    for index, book in enumerate(books, start=1):
        course = f" [{book.course}]" if book.course else ""
        print(f"{index}. {book.title}{course}")
        print(_style(f"   {book.pages} 页 · {book.chunks} 片段 · id={book.id}", DIM))
    return 0


def _cmd_quiz(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    quiz = buddy.quiz(book=args.book, topic=args.topic, count=args.count, difficulty=args.difficulty)
    if not quiz.questions:
        print("没有生成出题目，换一个主题或教材试试。", file=sys.stderr)
        return 1
    print(quiz.render(show_answers=args.answers))
    print(_style(f'提示: 做完后用 studybuddy grade "{quiz.topic}" --correct/--wrong 记录结果', DIM))
    return 0


def _cmd_review(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    items = buddy.review(limit=args.limit)
    if not items:
        print("今天没有到期的复习任务。👍")
        return 0
    print(_style("根据遗忘曲线，今天该复习这些:", BOLD))
    for index, item in enumerate(items, start=1):
        print(f"\n{index}. {item.topic}  " + _style(f"(掌握度 {item.mastery:.0%})", DIM))
        print(f"   {item.question}")
    print()
    print(_style('做完后: studybuddy grade "话题" --correct', DIM))
    return 0


def _cmd_memory(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    memory = buddy.memory
    if args.clear:
        memory.facts.clear()
        memory.episodes.clear()
        memory.topics.clear()
        memory.save()
        print("已清空记忆（画像保留）。")
        return 0
    if args.forget:
        removed = memory.remove_fact(args.forget)
        print("已删除。" if removed else f"没有编号为 {args.forget} 的记忆。")
        return 0

    print(memory.digest())
    return 0


def _cmd_profile(buddy: StudyBuddy, args: argparse.Namespace) -> int:
    if not any([args.name, args.major, args.grade_level, args.course, args.goal, args.pref]):
        print(
            buddy.memory.profile.summary()
            or "还没有设置画像。试试: studybuddy profile --major 计算机 --course 高等数学"
        )
        return 0

    profile = buddy.memory.set_profile(
        name=args.name,
        major=args.major,
        grade=args.grade_level,
        courses=args.course,
        goals=args.goal,
        preferences=args.pref,
    )
    print("已保存画像:")
    print(profile.summary())
    return 0


def _cmd_stats(buddy: StudyBuddy, _args: argparse.Namespace) -> int:
    stats = buddy.library.stats()
    memory = buddy.memory.stats()
    print(f"书库: {stats['books']} 本, {stats['pages']} 页, {stats['chunks']} 个片段")
    if stats["embeddings"]:
        print(f"向量: {stats['embeddings']} 个片段已索引（{stats['embedding_model']}）")
    else:
        print(_style("向量: 未建立（studybuddy index 可启用混合检索）", DIM))
    detail = (
        f"记忆: {memory['facts']} 条事实, {memory['episodes']} 条提问, "
        f"{memory['topics']} 个话题, {memory['due']} 个到期"
    )
    print(detail)
    print(_style(f"数据目录: {stats['root']}", DIM))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        buddy = StudyBuddy(
            root=args.root,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            embedding_model=args.embed_model,
            embedding_base_url=args.embed_base_url,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 1

    handlers = {
        "add": _cmd_add,
        "books": _cmd_books,
        "add-text": _cmd_add_text,
        "quiz": _cmd_quiz,
        "review": _cmd_review,
        "memory": _cmd_memory,
        "profile": _cmd_profile,
        "stats": _cmd_stats,
    }

    try:
        if args.command in handlers:
            return handlers[args.command](buddy, args)

        if args.command == "remove":
            book = buddy.library.remove_book(args.book)
            print(f"已删除 {book.title}")
            return 0

        if args.command == "ask":
            answer = buddy.ask(
                " ".join(args.question),
                books=args.book,
                top_k=args.top_k,
                remember=not args.no_memory,
                semantic=not args.no_semantic,
            )
            _print_answer(answer, quiet=args.quiet)
            return 0

        if args.command == "summary":
            answer = buddy.summary(book=args.book, chapter=args.chapter)
            print(answer.text)
            if answer.sources:
                print()
                _rule("来源")
                for source in answer.sources:
                    print(f"  [{source.index}] {source.citation}")
            return 0

        if args.command == "compare":
            result = buddy.compare(args.concept_a, args.concept_b)
            _warn(result.warnings)
            print(result.with_citations())
            return 0

        if args.command == "index":

            def embed_progress(done: int, total: int) -> None:
                print(_style(f"  向量化 {done}/{total}", DIM))

            added = buddy.build_index(force=args.force, on_progress=embed_progress)
            model = buddy.library.embeddings_model
            if added:
                print(f"✓ 已为 {added} 个片段建立向量索引（{model}）")
            else:
                print(f"✓ 向量索引已是最新（{model}）")
            return 0

        if args.command == "grade":
            state = buddy.grade(args.topic, correct=bool(args.correct))
            print(f"{state.name}: 掌握度 {state.mastery:.0%}, 下次复习 {state.due[:10]}")
            return 0

        parser.print_help()
        return 0
    except StudyError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
