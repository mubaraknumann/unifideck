"""GameVault install markers — the record that a game is installed here.

One JSON file per game id. Split out of ``install.py`` to keep it under the
550-LOC volumetry cap; the marker is also the file that outlives an install,
so it reads better as its own small module than as a tail section of the
download pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _marker_dir() -> Path:
    """Where the install markers live, resolved on every call.

    Not a module-level constant. One would be captured at import, before
    pytest's autouse fixture redirects ``HOME``, and the suite would then
    write marker files into the real user's data directory — the leak
    ``tests/conftest.py`` exists to catch and the trap
    ``launcher.wrapper_session.prefix_index_path`` documents. Reading the
    environment at call time is both correct and cheap.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "gamevault_installed"


def _marker_path(game_id: str) -> Path:
    return _marker_dir() / f"{game_id}.json"


def save_install_info(
    game_id: str,
    *,
    title: str,
    install_path: str,
    exe_path: str,
    archive_path: str = "",
) -> None:
    """Record that *game_id* is installed at *install_path*.

    ``archive_path`` is the file the install came from, recorded for
    diagnostics: it is how you tell from the marker alone which of two
    same-named archives in a vault actually produced this install. Nothing
    branches on it — uninstall removes ``install_path`` and never consults it.
    """
    try:
        _marker_dir().mkdir(parents=True, exist_ok=True)
        _marker_path(game_id).write_text(
            json.dumps(
                {
                    "game_id": game_id,
                    "title": title,
                    "install_path": install_path,
                    "exe_path": exe_path,
                    "archive_path": archive_path,
                },
                indent=2,
            )
        )
    except Exception:
        logger.exception("[GameVaultInstaller] Could not save marker")


def load_install_info(game_id: str) -> dict[str, Any] | None:
    p = _marker_path(game_id)
    try:
        if p.exists():
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:
        logger.debug("[GameVaultInstaller] Could not read marker: %s", exc)
    return None


def remove_install_info(game_id: str) -> None:
    p = _marker_path(game_id)
    try:
        if p.exists():
            p.unlink()
    except Exception as exc:
        logger.warning("[GameVaultInstaller] Could not remove marker: %s", exc)


def load_all_install_info() -> dict[str, dict[str, Any]]:
    """``{game_id: marker}`` for every readable marker on disk.

    Lives here rather than in ``install.py`` so that every read of the
    marker directory goes through this module. When the caller resolved
    ``_marker_dir`` itself, a test (or anything else) patching it here had
    no effect on that caller — one binding per fact is the point of the
    split.
    """
    result: dict[str, dict[str, Any]] = {}
    marker_dir = _marker_dir()
    if not marker_dir.exists():
        return result
    for f in marker_dir.glob("*.json"):
        try:
            loaded = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            # One unreadable marker must not hide every other installed
            # game, but it should be visible: this is how a game silently
            # reads as not-installed.
            logger.warning(
                "[GameVault markers] skipping unreadable marker %s: %s",
                f.name, exc,
            )
            continue
        if isinstance(loaded, dict):
            result[f.stem] = loaded
    return result
