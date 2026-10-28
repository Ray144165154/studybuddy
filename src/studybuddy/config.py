"""Non-secret settings shared by the CLI and the web UI.

The model and endpoint are remembered in ``<data root>/ui-config.json`` so a
student does not have to re-state them on every launch. The API key is never
stored here -- or anywhere else on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["CONFIG_FILE", "load", "save", "apply_to"]

CONFIG_FILE = "ui-config.json"


def path_for(root: str | Path) -> Path:
    return Path(root) / CONFIG_FILE


def load(root: str | Path) -> dict[str, str]:
    """Read the saved model choice; an unreadable file is simply ignored."""
    path = path_for(root)
    if not path.is_file():
        return {}
    try:
        data: Any = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value.strip()
        for key in ("model", "base_url")
        if isinstance(value := data.get(key), str) and value.strip()
    }


def save(root: str | Path, *, model: str, base_url: str) -> bool:
    """Persist the model choice. Returns False when the directory is read-only."""
    path = path_for(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"model": model, "base_url": base_url}, ensure_ascii=False, indent=2),
            "utf-8",
        )
        return True
    except OSError:
        return False


def apply_to(buddy: Any) -> None:
    """Overlay the saved model choice onto a StudyBuddy instance."""
    saved = load(getattr(buddy, "root", "."))
    if not saved:
        return
    if "model" in saved:
        buddy.model = saved["model"]
        buddy.llm.model = buddy.model
    if "base_url" in saved:
        buddy.llm.base_url = saved["base_url"]
