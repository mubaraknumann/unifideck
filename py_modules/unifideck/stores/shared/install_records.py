"""Per-store archive install records, independent of connection state.

One JSON file per game id under ``<data>/unifideck/<store>_installed/``. The
record is deliberately separate from the store's connection config: a
disconnect, a logout, or a server that stops answering must not lose the
knowledge of what is installed on disk, which the library overlay, uninstall
and Change Executable all read.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class InstallRecords:
    """Read and write one store's install records.

    ``store`` becomes part of every path this class builds, so it is validated
    once here rather than trusted at each call site.
    """

    def __init__(self, store: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", store):
            raise ValueError("Invalid store namespace")
        self._store = store

    def _directory(self) -> Path:
        """Where this store's records live, resolved on every call.

        Not computed at import or in ``__init__``: tests build instances at
        import time, before ``tests/conftest.py`` unsets ``XDG_DATA_HOME`` and
        redirects ``HOME``, so a cached path would point at the real user's
        data directory.
        """
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "unifideck" / f"{self._store}_installed"

    def _path(self, game_id: str) -> Path:
        """The record file for *game_id*, refusing anything that could escape.

        The character class is a containment rule as much as a naming one: a
        game id arrives from a store's API, the first character must be
        alphanumeric, and no path separator can appear in the name built here.
        """
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", game_id):
            raise ValueError("Invalid install record ID")
        return self._directory() / f"{game_id}.json"

    def save(
        self,
        game_id: str,
        *,
        title: str,
        install_path: str,
        exe_path: str,
        archive_path: str = "",
    ) -> None:
        """Write *game_id*'s record, replacing any previous one atomically.

        The content goes to a temporary file beside it and is renamed into
        place: a half-written record would read back as "not installed", and
        the directory it names would then be unreclaimable from the UI.
        """
        path = self._path(game_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "game_id": game_id,
            "title": title,
            "install_path": install_path,
            "exe_path": exe_path,
            "archive_path": archive_path,
        }
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".record-")
        temp = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, indent=2)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self, game_id: str) -> dict[str, Any] | None:
        """The stored record, or ``None`` when it is absent or unreadable.

        A damaged record and a missing one mean the same thing to every
        caller: this game is not installed as far as we can tell.
        """
        path = self._path(game_id)
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def remove(self, game_id: str) -> None:
        """Forget *game_id*. Absent is success."""
        self._path(game_id).unlink(missing_ok=True)

    def load_all(self) -> dict[str, dict[str, Any]]:
        """``{game_id: record}`` for this store, skipping unreadable files.

        Never raises, and warns instead: one damaged record must not abort the
        library read that overlays it.
        """
        result = {}
        for path in self._directory().glob("*.json"):
            try:
                data = self.load(path.stem)
            except ValueError:
                data = None
            if data is not None:
                result[path.stem] = data
            else:
                logger.warning("[%s installs] skipping unreadable record %s", self._store, path.name)
        return result
