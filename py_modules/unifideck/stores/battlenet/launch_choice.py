"""The launch popup's answer, handed from the frontend to the launcher.

py_modules/unifideck/stores/battlenet/launch_choice.py

Play on a Battle.net tile shows Steam's launch-options dialog (installed
versions plus the Battle.net client). The frontend records the pick through
the backend, *then* starts the shortcut, and the launcher consumes it here.
Nothing travels in the shortcut's launch options: Steam applies
``SetShortcutLaunchOptions`` asynchronously, and a token set just before
``RunGame`` was measured not to arrive.

One file, keyed by game id, each entry used at most once and only while
fresh. A stale entry (the user cancelled, or Steam never started the
shortcut) must not hijack a later launch from outside the popup.

Stdlib only: the launcher reads this under the system Python. The path is
resolved per call, like ``battlenet_client.id_map_path``, so tests that
redirect ``HOME`` never touch the developer's real data directory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: The choice that opens the Battle.net client alone, no game.
CLIENT = "client"
#: How long a recorded choice stays valid. The popup records it and starts
#: the shortcut at once, so the launcher normally reads it within seconds.
FRESH_S = 120.0


def choice_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "battlenet_launch_choice.json"


def _load(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    Path(tmp).replace(path)


def record(game_id: str, choice: str, now: float | None = None) -> None:
    """Remember ``choice`` (a version uid, or :data:`CLIENT`) for one launch."""
    path = choice_path()
    data = _load(path)
    data[game_id] = {"choice": choice, "ts": time.time() if now is None else now}
    _save(path, data)


def consume(game_id: str, now: float | None = None) -> str | None:
    """The fresh choice for ``game_id``, removed so it is used only once.

    Never raises: a launch without a readable choice behaves exactly as a
    launch from outside the popup.
    """
    path = choice_path()
    try:
        data = _load(path)
        entry = data.pop(game_id, None)
        if entry is None:
            return None
        _save(path, data)
    except OSError as exc:
        logger.warning("[battlenet] cannot read the launch choice at %s: %s", path, exc)
        return None
    stamp = entry.get("ts")
    recorded = float(stamp) if isinstance(stamp, int | float) else 0.0
    age = (time.time() if now is None else now) - recorded
    choice = entry.get("choice")
    if not isinstance(choice, str) or not choice or not 0 <= age <= FRESH_S:
        logger.info("[battlenet] ignoring a stale launch choice for %s (age %.0fs)", game_id, age)
        return None
    return choice
