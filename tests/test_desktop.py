"""Desktop window: server lifecycle, browser discovery, and the headless self-check.

The window itself needs a display, so the GUI backends are covered by their pure
parts (argv building, browser lookup) plus the server they point at.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from studybuddy import Book, Chunk, Library, Memory, StudyBuddy
from studybuddy.cli import _default_root, _parse_size
from studybuddy.desktop import (
    DesktopApp,
    app_window_command,
    default_argv,
    find_chromium,
    run_desktop,
)
from studybuddy.errors import StudyError


class DesktopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "data"
        library = Library(self.root)
        library.books = [Book(id="m1", title="概率论基础", course="概率论", pages=10, chunks=1)]
        library.chunks = [
            Chunk(
                id="m1:p10:0",
                book_id="m1",
                book_title="概率论基础",
                page=10,
                section="1.1 条件概率",
                text="条件概率 P(A|B) 是在事件 B 发生的条件下事件 A 发生的概率。",
            )
        ]
        library.save()
        self.library = library
        self.memory = Memory(self.root)

    def buddy(self) -> StudyBuddy:
        return StudyBuddy(
            self.root,
            model="test-model",
            api_key="not-needed",
            library=self.library,
            memory=self.memory,
        )


class TestDefaultArgv(unittest.TestCase):
    """The packaged binary is a full CLI, but a double-click must open the window."""

    def test_bare_launch_opens_desktop(self) -> None:
        self.assertEqual(default_argv([]), ["desktop"])

    def test_desktop_flag_gets_the_implicit_command(self) -> None:
        self.assertEqual(default_argv(["--check"]), ["desktop", "--check"])
        self.assertEqual(default_argv(["--size", "1000x700"]), ["desktop", "--size", "1000x700"])

    def test_root_options_are_left_alone(self) -> None:
        self.assertEqual(default_argv(["--root", "D:/data", "ui"]), ["--root", "D:/data", "ui"])

    def test_known_command_is_left_alone(self) -> None:
        self.assertEqual(default_argv(["ask", "导数"]), ["ask", "导数"])


class TestAppWindowCommand(unittest.TestCase):
    def test_command_opens_app_mode_with_private_profile(self) -> None:
        profile = Path("/tmp/profile")
        command = app_window_command(
            "/usr/bin/chromium",
            "http://127.0.0.1:5000",
            profile,
            width=1000,
            height=700,
        )
        self.assertEqual(command[0], "/usr/bin/chromium")
        self.assertIn("--app=http://127.0.0.1:5000", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--window-size=1000,700", command)
        # First-run chrome would steal focus from the app on a fresh profile.
        self.assertIn("--no-first-run", command)

    def test_env_override_wins(self) -> None:
        with patch.dict(os.environ, {"STUDYBUDDY_BROWSER": sys.executable}):
            self.assertEqual(find_chromium(), sys.executable)

    def test_env_override_must_exist(self) -> None:
        with patch.dict(os.environ, {"STUDYBUDDY_BROWSER": str(Path(tempfile.gettempdir()) / "no-such")}):
            self.assertIsNone(find_chromium())


class TestDesktopServer(DesktopTestCase):
    def test_free_port_and_state_endpoint(self) -> None:
        app = DesktopApp(self.buddy(), port=0)
        self.addCleanup(app.stop)
        url = app.start()
        self.assertNotIn(":0", url)

        with urllib.request.urlopen(f"{url}/api/state", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["books"][0]["title"], "概率论基础")
        self.assertEqual(payload["stats"]["chunks"], 1)

    def test_start_is_idempotent(self) -> None:
        app = DesktopApp(self.buddy(), port=0)
        self.addCleanup(app.stop)
        self.assertEqual(app.start(), app.start())

    def test_stop_twice_is_safe(self) -> None:
        app = DesktopApp(self.buddy(), port=0)
        app.start()
        app.stop()
        app.stop()

    def test_context_manager_stops_the_server(self) -> None:
        with DesktopApp(self.buddy(), port=0) as app:
            url = app.url
            with urllib.request.urlopen(f"{url}/api/state", timeout=10) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(OSError):
            urllib.request.urlopen(f"{url}/api/state", timeout=2)

    def test_page_load_is_observable(self) -> None:
        # The window backend decides "did the browser really open our page" from
        # this signal, so a silent server would silently break the fallback chain.
        with DesktopApp(self.buddy(), port=0) as app:
            self.assertFalse(app.wait_for_load(0.2))
            with urllib.request.urlopen(app.url, timeout=10) as response:
                self.assertEqual(response.status, 200)
            self.assertTrue(app.wait_for_load(5))
            app.reset_load()
            self.assertFalse(app.wait_for_load(0.2))


class TestRunDesktop(DesktopTestCase):
    def test_check_mode_returns_zero(self) -> None:
        self.assertEqual(run_desktop(self.buddy(), port=0, check=True), 0)

    def test_unknown_native_mode_is_rejected(self) -> None:
        with self.assertRaises(StudyError):
            run_desktop(self.buddy(), port=0, native="telepathy")


class TestCliHelpers(unittest.TestCase):
    def test_size_parsing(self) -> None:
        self.assertEqual(_parse_size("1280x900"), (1280, 900))
        self.assertEqual(_parse_size("1000*700"), (1000, 700))

    def test_size_rejects_junk(self) -> None:
        for value in ("1280", "bigx900", "100x100"):
            with self.assertRaises(StudyError, msg=value):
                _parse_size(value)

    def test_default_root_honours_env(self) -> None:
        with patch.dict(os.environ, {"STUDYBUDDY_ROOT": r"D:\books\data"}):
            self.assertEqual(_default_root(), r"D:\books\data")

    def test_frozen_default_root_is_per_user(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\ray\AppData\Local"}, clear=False):
            os.environ.pop("STUDYBUDDY_ROOT", None)
            with patch("sys.frozen", True, create=True):
                self.assertEqual(_default_root(), str(Path(r"C:\Users\ray\AppData\Local") / "studybuddy"))


if __name__ == "__main__":
    unittest.main()
