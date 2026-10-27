"""CLI wiring: commands reach the right layer and exit codes are honest."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from studybuddy.cli import main
from support import make_textbook


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = str(self.tmp / "data")
        self.pdf = make_textbook(self.tmp / "calculus.pdf")

    def test_help_without_command(self) -> None:
        code, out, _ = run()
        self.assertEqual(code, 0)
        self.assertIn("studybuddy", out)

    def test_books_empty(self) -> None:
        code, out, _ = run("--root", self.root, "books")
        self.assertEqual(code, 0)
        self.assertIn("书库是空的", out)

    def test_add_and_list(self) -> None:
        code, out, _ = run("--root", self.root, "add", str(self.pdf), "--course", "高等数学")
        self.assertEqual(code, 0)
        self.assertIn("导入", out + "导入")  # the banner is a check mark line
        self.assertIn("calculus", out)

        code, out, _ = run("--root", self.root, "books")
        self.assertEqual(code, 0)
        self.assertIn("calculus", out)
        self.assertIn("高等数学", out)

    def test_books_json(self) -> None:
        run("--root", self.root, "add", str(self.pdf), "--title", "高等数学")
        code, out, _ = run("--root", self.root, "books", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["title"], "高等数学")

    def test_add_missing_file_fails(self) -> None:
        code, _, err = run("--root", self.root, "add", str(self.tmp / "nope.pdf"))
        self.assertEqual(code, 1)
        self.assertIn("找不到文件", err)

    def test_stats(self) -> None:
        run("--root", self.root, "add", str(self.pdf))
        code, out, _ = run("--root", self.root, "stats")
        self.assertEqual(code, 0)
        self.assertIn("书库: 1 本", out)
        self.assertIn("记忆:", out)

    def test_profile_set_and_show(self) -> None:
        code, out, _ = run("--root", self.root, "profile", "--major", "计算机", "--course", "高等数学")
        self.assertEqual(code, 0)
        self.assertIn("计算机", out)

        code, out, _ = run("--root", self.root, "profile")
        self.assertEqual(code, 0)
        self.assertIn("高等数学", out)

    def test_profile_empty(self) -> None:
        code, out, _ = run("--root", self.root, "profile")
        self.assertEqual(code, 0)
        self.assertIn("还没有设置", out)

    def test_grade(self) -> None:
        code, out, _ = run("--root", self.root, "grade", "极限", "--correct")
        self.assertEqual(code, 0)
        self.assertIn("极限", out)
        self.assertIn("掌握度", out)

    def test_memory_digest(self) -> None:
        run("--root", self.root, "grade", "极限", "--correct")
        code, out, _ = run("--root", self.root, "memory")
        self.assertEqual(code, 0)
        self.assertIn("极限", out)

    def test_memory_clear(self) -> None:
        run("--root", self.root, "grade", "极限", "--correct")
        code, out, _ = run("--root", self.root, "memory", "--clear")
        self.assertEqual(code, 0)
        self.assertIn("已清空", out)

    def test_memory_forget_unknown(self) -> None:
        code, out, _ = run("--root", self.root, "memory", "--forget", "f9999")
        self.assertEqual(code, 0)
        self.assertIn("没有编号", out)

    def test_ask_on_empty_library(self) -> None:
        code, _, err = run("--root", self.root, "-k", "test", "ask", "什么是导数")
        self.assertEqual(code, 1)
        self.assertIn("书库是空的", err)

    def test_remove(self) -> None:
        run("--root", self.root, "add", str(self.pdf), "--title", "高等数学")
        code, out, _ = run("--root", self.root, "remove", "高等数学")
        self.assertEqual(code, 0)
        self.assertIn("已删除", out)
        _, out, _ = run("--root", self.root, "books")
        self.assertIn("书库是空的", out)

    def test_remove_unknown_book(self) -> None:
        run("--root", self.root, "add", str(self.pdf), "--title", "高等数学")
        code, _, err = run("--root", self.root, "remove", "物理")
        self.assertEqual(code, 1)
        self.assertIn("错误", err)


if __name__ == "__main__":
    unittest.main()
