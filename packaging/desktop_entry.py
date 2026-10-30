"""Entry point for the packaged desktop build.

PyInstaller freezes this file instead of the console script: double-clicking the
resulting binary has no argv, so the desktop window is the default command.

The binary is built windowed (no console), where ``sys.stdout`` is ``None``.
Every ``print`` in the CLI would then raise, so they are redirected to a sink
before the CLI runs.
"""

from __future__ import annotations

import io
import sys

from studybuddy.cli import main
from studybuddy.desktop import default_argv

if __name__ == "__main__":
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, io.StringIO())
    sys.exit(main(default_argv(sys.argv[1:])))
