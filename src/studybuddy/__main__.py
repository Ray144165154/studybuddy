"""Allow ``python -m studybuddy`` as an alias for the ``studybuddy`` command."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
