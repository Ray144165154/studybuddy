"""Entry point for the packaged desktop build.

PyInstaller freezes this file instead of the console script: double-clicking the
resulting binary has no argv, so the desktop window is the default command.

The binary is built windowed (no console), where ``sys.stdout`` is ``None``.
Every ``print`` in the CLI would then raise, so they are redirected to a persistent diagnostic log
before the CLI runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from studybuddy.cli import _default_root, main
from studybuddy.desktop import default_argv

if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:
        log_dir = Path(_default_root()) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "desktop.log").open("a", encoding="utf-8", buffering=1)
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                setattr(sys, name, log)
    sys.exit(main(default_argv(sys.argv[1:])))
