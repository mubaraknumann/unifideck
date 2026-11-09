"""Resolve a human game title for launcher toasts.

py_modules/unifideck/launcher/game_title.py

Launcher toasts had nothing better to show than the raw launch key, so a
Battle.net toast read *"Starting battlenet:D1 through Battle.net…"* and a
Ubisoft one named a bare UUID. ``games.map`` cannot help — its rows are
``store:game_id = exe \\t work_dir \\t app_id`` with no title column — but
``shortcuts_registry.json`` records one per shortcut, because the reconcile
pass needs it to name the Steam entry.

Read-only, stdlib-only, and never raises: this runs in the out-of-process
launcher under the SYSTEM python (3.10-3.14), and a missing or malformed
registry must degrade to "no nicer title available" rather than break a
launch that is otherwise fine.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")),
) / "unifideck"
REGISTRY_PATH = _DATA_DIR / "shortcuts_registry.json"


def registered_title(game_key: str, *, registry_path: Path | None = None) -> str:
    """The title the registry records for ``store:game_id``, or ``""``."""
    path = registry_path or REGISTRY_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    entry = data.get(game_key)
    title = entry.get("title") if isinstance(entry, dict) else None
    return title if isinstance(title, str) else ""


def resolve_title(game_key: str, *, registry_path: Path | None = None) -> str:
    """The display title for ``store:game_id``, or ``game_key`` unchanged.

    Falling back to the key rather than to an empty string is deliberate:
    a toast reading "Starting battlenet:D1" is poor, but one reading
    "Starting" is broken.
    """
    return registered_title(game_key, registry_path=registry_path) or game_key
