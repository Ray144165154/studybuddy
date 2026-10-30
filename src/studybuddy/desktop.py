"""Run the web UI in its own window instead of a browser tab.

Three backends, tried in order:

1. **pywebview** (``pip install "studybuddy[desktop]"``) -- a real native window
   with no browser chrome: WebView2 on Windows, WKWebView on macOS, WebKitGTK/Qt
   on Linux. Nothing else to install, no browser profile touched.
2. **Chromium app mode** -- ``msedge --app=...`` / ``chrome --app=...`` with a
   private profile under the data directory. Still a standalone window with its
   own taskbar entry, but zero extra Python dependencies, and Edge ships with
   Windows 10+.
3. **The default browser** -- identical to ``studybuddy ui``; the terminal keeps
   serving until Ctrl-C.

The first two block until the user closes the window, so the HTTP server lives
exactly as long as the window does. Killing the window therefore cannot leave a
stray process listening on a port.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .errors import StudyError
from .study import StudyBuddy
from .web import WebApp, make_server

__all__ = [
    "DesktopApp",
    "app_window_command",
    "default_argv",
    "find_chromium",
    "run_desktop",
]

# Commands the CLI understands; used to decide whether a bare argv needs the
# implicit ``desktop`` subcommand prepended.
KNOWN_COMMANDS = frozenset(
    {
        "add",
        "add-text",
        "ask",
        "books",
        "compare",
        "desktop",
        "doctor",
        "grade",
        "index",
        "memory",
        "profile",
        "quiz",
        "remove",
        "review",
        "stats",
        "summary",
        "ui",
    }
)
# Flags only the desktop subcommand accepts, so ``StudyBuddy.exe --check`` works.
DESKTOP_ONLY_FLAGS = ("--check", "--size", "--native", "--port", "-p")


def default_argv(argv: Sequence[str]) -> list[str]:
    """Give a double-clicked build an explicit command.

    Launching the packaged binary has no arguments at all, and a desktop flag on
    its own (``StudyBuddy.exe --check``) would otherwise be rejected by argparse
    because root options must come *before* the subcommand. Anything else is
    passed through untouched, so the binary is still a full CLI.
    """
    args = list(argv)
    if not args:
        return ["desktop"]
    if args[0] in KNOWN_COMMANDS or args[0] not in DESKTOP_ONLY_FLAGS:
        return args
    return ["desktop", *args]


# Edge and Chrome both accept these; they keep a fresh profile from showing the
# first-run import wizard, which would otherwise steal focus on first launch.
_APP_WINDOW_FLAGS = (
    "--app={url}",
    "--user-data-dir={profile}",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size={width},{height}",
)

_WINDOW_LOAD_TIMEOUT = 8.0  # seconds to wait for the browser to fetch the page

_WINDOWS_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_MACOS_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
_LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
)


def find_chromium() -> str | None:
    """Return a Chromium-based browser path, or ``None``.

    ``STUDYBUDDY_BROWSER`` wins so a user can point at a portable build.
    """
    override = os.environ.get("STUDYBUDDY_BROWSER", "").strip()
    if override:
        return override if Path(override).exists() else None

    if sys.platform == "win32":
        for candidate in _WINDOWS_BROWSERS:
            if Path(candidate).is_file():
                return candidate
    elif sys.platform == "darwin":
        for candidate in _MACOS_BROWSERS:
            if Path(candidate).is_file():
                return candidate
    else:
        for name in _LINUX_BROWSERS:
            found = shutil.which(name)
            if found:
                return found
    return None


def app_window_command(
    browser: str,
    url: str,
    profile: Path,
    *,
    width: int = 1280,
    height: int = 900,
    extra: Sequence[str] = (),
) -> list[str]:
    """The argv that opens ``url`` as an app window with an isolated profile."""
    flags = [flag.format(url=url, profile=profile, width=width, height=height) for flag in _APP_WINDOW_FLAGS]
    return [browser, *flags, *extra]


class DesktopApp:
    """The HTTP server behind the window, startable and stoppable from code."""

    def __init__(self, buddy: StudyBuddy, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._loaded = threading.Event()
        self._server, self._app, self.url = make_server(
            buddy,
            host=host,
            port=port,
            on_request=lambda _path: self._loaded.set(),
        )
        self._thread: threading.Thread | None = None

    @property
    def app(self) -> WebApp:
        return self._app

    def reset_load(self) -> None:
        self._loaded.clear()

    def wait_for_load(self, timeout: float) -> bool:
        """Did any client fetch the page within ``timeout`` seconds?"""
        return self._loaded.wait(timeout)

    def start(self) -> str:
        if self._thread is not None:
            return self.url
        # daemon=True: a window that fails to open must never keep the process alive.
        self._thread = threading.Thread(target=self._server.serve_forever, name="studybuddy-web", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._app.close()
        self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> DesktopApp:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _open_native_window(url: str, *, width: int, height: int) -> bool:
    """Show ``url`` in a pywebview window. ``False`` when pywebview cannot run.

    pywebview raises from deep inside its GUI backend when the platform pieces are
    missing (a broken pythonnet/.NET install on Windows, no GTK/Qt on Linux), so the
    whole attempt is guarded: a broken optional dependency must degrade to the
    browser window, never take the app down.
    """
    try:
        webview: Any = importlib.import_module("webview")
        webview.create_window(
            f"studybuddy {__version__}",
            url,
            width=width,
            height=height,
            min_size=(960, 600),
            text_select=True,
        )
        webview.start()
    except Exception as exc:  # noqa: BLE001
        print(f"pywebview 不可用（{exc}），改用浏览器窗口。", file=sys.stderr, flush=True)
        return False
    return True


def _await_close(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Kill the browser *and its children*.

    ``Popen.terminate()`` only reaches the launcher process on Windows; the actual
    browser survives, keeps the profile directory locked and would adopt the retry
    instead of opening our window, so the retry silently does nothing.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _open_app_window(
    launcher: DesktopApp,
    url: str,
    profile: Path,
    *,
    width: int,
    height: int,
) -> bool:
    """Show ``url`` in a Chromium app window. ``False`` when that cannot be done.

    The success signal is the server seeing a request: a window is only accepted
    once the page has actually been fetched, which rules out "the browser opened
    some unrelated tab" (and stale windows from earlier runs).

    Chromium also refuses to start its own sandbox inside an already-restricted
    parent (managed Windows setups, containers), where it silently opens a plain
    tab with a truncated URL. The first attempt stays sandboxed; if the page is
    never fetched, one retry adds ``--no-sandbox``, and only when that also fails
    does the caller fall back to the default browser.
    """
    browser = find_chromium()
    if browser is None:
        return False
    profile.mkdir(parents=True, exist_ok=True)

    for extra in ((), ("--no-sandbox",)):
        command = app_window_command(browser, url, profile, width=width, height=height, extra=extra)
        launcher.reset_load()
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        # A dedicated --user-data-dir gives us a private browser process whose
        # lifetime is exactly the window's, so wait() ends when the window closes.
        if launcher.wait_for_load(_WINDOW_LOAD_TIMEOUT):
            _await_close(process)
            return True
        _terminate(process)
    return False


def _check(buddy: StudyBuddy, app: DesktopApp) -> int:
    """Headless smoke test used by the packaged build and CI."""
    try:
        with urllib.request.urlopen(f"{app.url}/api/state", timeout=10) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"自检失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"自检通过: {payload.get('version')} · {app.url} · "
        f"{payload.get('stats', {}).get('books', 0)} 本教材 · 数据目录 {buddy.library.root}"
    )
    return 0


def run_desktop(
    buddy: StudyBuddy,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    native: str = "auto",
    width: int = 1280,
    height: int = 900,
    check: bool = False,
) -> int:
    """Serve the UI and open it in a window. Returns a process exit code."""
    if native not in ("auto", "webview", "browser"):
        raise StudyError(f"未知的窗口模式: {native}")

    app = DesktopApp(buddy, host=host, port=port)
    app.start()
    try:
        if check:
            return _check(buddy, app)

        print(f"studybuddy {__version__}  →  {app.url}", flush=True)
        print(f"数据目录: {buddy.library.root}", flush=True)
        print("关闭窗口即可退出", flush=True)

        if native in ("auto", "webview") and _open_native_window(app.url, width=width, height=height):
            return 0
        if native == "webview":
            raise StudyError('没有安装 pywebview，先运行: pip install "studybuddy[desktop]"')

        profile = Path(buddy.library.root) / "browser-profile"
        if native in ("auto", "browser") and _open_app_window(
            app, app.url, profile, width=width, height=height
        ):
            return 0

        # Last resort: no Chromium browser on this machine.
        print("没有找到 Edge / Chrome，改用系统默认浏览器。", file=sys.stderr, flush=True)
        __import__("webbrowser").open(app.url)
        print("按 Ctrl-C 退出", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\n再见")
        return 0
    finally:
        app.stop()
