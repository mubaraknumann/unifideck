"""Upgrade legacy ``.unifideck-id`` markers to the canonical JSON format.

Pre-v6 versions of Unifideck wrote install markers in two non-canonical
forms (raw integer id, ``{"id": ...}`` dict). This module sweeps the
download directory at library boot time and rewrites every legacy
marker into the canonical ``{"game_id": ..., "name": ..., ...}`` form,
enriched with metadata from the in-game ``goggame-<id>.info`` file
when present.

``_MarkerMigration`` exposes ``migrate_old_markers()`` which returns a
counter dict ``{"migrated": N, "skipped": M}``. Individual marker
failures are tolerated (counted as ``skipped``) so a single corrupted
marker doesn't block the whole library load.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .library import GOGLibrary
logger = logging.getLogger(__name__)
_INSTALL_MARKER = ".unifideck-id"

class _MarkerMigration:
    """Marker migration."""

    def __init__(self, parent: GOGLibrary) -> None:
        """Initialize the instance."""
        self._parent = parent

    def migrate_old_markers(self) -> dict[str, int]:
        """Migrate old markers."""
        migrated = 0
        skipped = 0
        download_dir = str(Path(self._parent._config.download_dir).expanduser())
        if not Path(download_dir).is_dir():
            return {"migrated": 0, "skipped": 0}
        try:
            for name in [entry.name for entry in Path(download_dir).iterdir()]:
                game_dir = str(Path(download_dir) / name)
                if not Path(game_dir).is_dir():
                    continue
                marker_path = str(Path(game_dir) / _INSTALL_MARKER)
                if not Path(marker_path).is_file():
                    continue
                outcome = self._migrate_one_marker(
                    game_dir,
                    marker_path,
                )
                if outcome == "migrated":
                    migrated += 1
                else:
                    skipped += 1
        except OSError:
            logger.exception("[GOGLibrary] migrate scan failed")
        logger.info(
            "[GOGLibrary] migration: %d upgraded, %d current",
            migrated,
            skipped,
        )
        return {"migrated": migrated, "skipped": skipped}

    def _migrate_one_marker(self, game_dir: str, marker_path: str) -> str:
        """Migrate one marker."""
        content = self._read_marker_content(marker_path)
        if content is None:
            return "failed"
        if self._marker_is_new_format(content):
            return "skipped"
        old_id = self._extract_legacy_id(content)
        if not old_id:
            return "skipped"
        new_data = self._build_new_marker_payload(
            game_dir,
            old_id,
        )
        return self._write_new_marker(
            marker_path,
            new_data,
            game_dir,
        )

    @staticmethod
    def _read_marker_content(marker_path: str) -> str | None:
        """Read marker content."""
        try:
            with Path(marker_path).open(encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def _marker_is_new_format(content: str) -> bool:
        """Marker is new format."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return False
        return isinstance(data, dict) and "game_id" in data

    @staticmethod
    def _extract_legacy_id(content: str) -> str | None:
        """Extract legacy ID."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, (str, int)):
            return str(data)
        if content and not content.startswith("{"):
            return content
        return None

    def _build_new_marker_payload(self, game_dir: str, old_id: str) -> dict[str, Any]:
        """Build new marker payload."""
        new_data: dict[str, Any] = {"game_id": old_id}
        for candidate in (
            game_dir,
            str(Path(game_dir) / "game"),
        ):
            if not Path(candidate).is_dir():
                continue
            info_file = self._find_first_goggame_info(candidate)
            if not info_file:
                continue
            with contextlib.suppress(OSError, json.JSONDecodeError):
                with Path(info_file).open(encoding="utf-8") as f:
                    new_data = json.load(f)
                new_data["game_id"] = old_id
            break
        return new_data

    @staticmethod
    def _write_new_marker(
        marker_path: str,
        new_data: dict[str, Any],
        game_dir: str,
    ) -> str:
        """Write new marker."""
        try:
            with Path(marker_path).open("w", encoding="utf-8") as f:
                json.dump(new_data, f, indent=2)
                return "migrated"
        except OSError as e:
            logger.warning(
                "[GOGLibrary] migrate write failed for %s: %s",
                game_dir,
                e,
            )
            return "failed"

    @staticmethod
    def _find_first_goggame_info(directory: str) -> str | None:
        """Find first goggame info."""
        candidates = sorted(
            str(p) for p in Path(directory).glob("goggame-*.info")
        )
        return candidates[0] if candidates else None
