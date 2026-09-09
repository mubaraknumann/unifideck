"""
Detect installed Ubisoft games — find on-disk games not in the registry.

When Unifideck is freshly installed or the user has manually copied
game files between prefixes, the install registry may be incomplete:
games may exist on disk that Unifideck doesn't know about.

``_LibraryDetection`` runs a discovery pass over every known install
location (UPC's ``games/`` dir + Unifideck's ``default_install_base``)
and identifies on-disk games that aren't in the registry. The detection
uses heuristics from ``detection_helpers.py`` and ``detection_cascade.py``
to handle the many corner cases (DRM-locked installs, partial installs,
renamed dirs, etc.).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.id_map import UbisoftIdMap

from .detection_cascade import _DetectionCascade
from .detection_helpers import (
    _DetectionHelpers,
    in_prefix_game_roots,
)
from .detection_helpers import (
    find_game_executable as _find_game_executable_impl,
)
from .detection_helpers import (
    load_json_file_safe as _load_json_file_safe_impl,
)
from .detection_helpers import (
    write_install_marker as _write_install_marker_impl,
)

logger = logging.getLogger(__name__)

class _InstallDetector:
    """Install detector."""

    def __init__(
        self,
        config: UbisoftConfig,
        id_map: UbisoftIdMap,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._id_map = id_map
        self._cascade = _DetectionCascade(self)
        self._helpers = _DetectionHelpers(self)

    @staticmethod
    def find_game_executable(
        install_path: str,
    ) -> str | None:
        """Find game executable."""
        return _find_game_executable_impl(install_path)

    async def write_install_marker(
        self,
        space_id: str,
        install_path: str,
        executable: str,
        game_title: str = "",
    ) -> None:
        """Write install marker."""
        await _write_install_marker_impl(
            space_id,
            install_path,
            executable,
            game_title,
        )

    @staticmethod
    def load_json_file_safe(path: str) -> Any | None:
        """Load JSON file safe."""
        return _load_json_file_safe_impl(path)

    @staticmethod
    def get_game_official_url(game_id: str) -> str:
        """Get game official URL."""
        return f"https://store.ubisoft.com/game?pid={game_id}"

    async def get_installed(self) -> dict[str, Any]:
        """Get installed.

        Scans the union of the internal ``prefixes_dir`` and any per-game
        prefixes relocated to SD / custom storage (recorded in the id_map),
        so games installed off-disk still flip Install→Play and appear
        installed in the library.
        """
        installed: dict[str, Any] = {}
        prefix_paths = await asyncio.to_thread(
            self._id_map.iter_all_game_prefix_paths,
        )
        for prefix_str in prefix_paths:
            entry = Path(prefix_str)
            if entry.name.startswith("."):
                continue
            if not await asyncio.to_thread(entry.is_dir):
                continue
            marker_path = entry / self._config.bootstrap_marker
            if not await asyncio.to_thread(marker_path.is_file):
                continue
            game_info = self._detect_installed_game(
                entry.name,
                str(entry),
            )
            if not game_info:
                continue
            installed[entry.name] = game_info
            await self._auto_resolve_missing_id(
                entry.name,
                str(entry),
                game_info,
            )
        return installed

    def get_installed_game_info(
        self,
        game_id: str,
    ) -> dict[str, Any] | None:
        """Get installed game info."""
        prefix_path = Path(
            self._id_map.resolve_prefix_path(game_id)
            or str(Path(self._config.prefixes_dir_expanded) / game_id),
        )
        if not prefix_path.is_dir():
            return None
        marker_path = prefix_path / self._config.bootstrap_marker
        if not marker_path.is_file():
            return None
        info = self._detect_installed_game(
            game_id,
            str(prefix_path),
        )
        if info:
            self._auto_resolve_id_from_registry(
                game_id,
                str(prefix_path),
                info,
            )
        return info

    def _auto_resolve_id_from_registry(
        self,
        space_id: str,
        prefix_path: str,
        game_info: dict[str, Any],
    ) -> None:
        """Adopt the installed game's own registry id, and cross-check it.

        The game's prefix carries its ``Launcher\\Installs\\<id>`` key, so
        for an installed game this is unambiguous evidence — unlike UPC's
        shared leveldb cache, which #436 showed can hand a game its
        neighbour's id. Runs even when an id is already recorded: a
        weaker-sourced value that disagrees is corrected here — but not
        once the recorded id already came from the registry (or a hand
        edit), which also spares a re-read of ``system.reg`` per game
        on every later sync.
        """
        existing = self._id_map.get_entry(space_id)
        if existing.get("ubisoftconnect_game_id_source") in ("registry", "manual"):
            return
        reg_id = UbisoftIdMap.extract_game_id_from_registry(
            prefix_path,
        )
        if not reg_id:
            return
        fields: dict[str, Any] = {"name": game_info.get("title", "")}
        if not existing.get("launch_id"):
            fields["install_id"] = reg_id
            fields["launch_id"] = reg_id
        if self._id_map.set_connect_id(space_id, reg_id, "registry", fields):
            logger.info(
                "[UbisoftLibrary] resolved game ID for %s from its registry: %s",
                space_id,
                reg_id,
            )

    async def _auto_resolve_missing_id(
        self,
        space_id: str,
        prefix_path: str,
        game_info: dict[str, Any],
    ) -> None:
        """Auto resolve missing ID."""
        existing = self._id_map.get_entry(space_id)
        if existing.get("launch_id") or existing.get("ubisoftconnect_game_id"):
            return
        source = "registry"
        reg_id = UbisoftIdMap.extract_game_id_from_registry(
            prefix_path,
        )
        if not reg_id:
            game_title = game_info.get("title", "")
            source = "name_db"
            if game_title:
                reg_id = await self._id_map.lookup_game_id_by_name(
                    game_title,
                )
        if not reg_id:
            return
        self._id_map.set_connect_id(
            space_id,
            reg_id,
            source,
            {
                "install_id": reg_id,
                "launch_id": reg_id,
                "name": game_info.get("title", ""),
            },
        )
        logger.info(
            "[UbisoftLibrary] auto-resolved game ID for %s: %s",
            space_id,
            reg_id,
        )

    def _detect_installed_game(
        self,
        space_id: str,
        prefix_path: str,
    ) -> dict[str, Any] | None:
        """Detect installed game."""
        try:
            from unifideck.stores.ubisoft.parser import check_install_state
        except ImportError as e:
            logger.debug(
                "[UbisoftLibrary] ubisoft_parser unavailable: %s",
                e,
            )
            return None
        known_name = self._get_game_name(space_id) or ""
        normalized_known_name = (
            self._id_map.normalize_for_matching(known_name) if known_name else ""
        )
        prefix_game_roots = in_prefix_game_roots(prefix_path)
        external_game_roots = self._helpers.get_external_game_roots()
        method1 = self._cascade.detect_via_marker(
            space_id,
            known_name,
            [*prefix_game_roots, *external_game_roots],
        )
        if method1:
            return method1
        method2 = self._cascade.detect_via_prefix_install_state(
            space_id,
            prefix_game_roots,
            normalized_known_name,
            known_name,
            check_install_state,
        )
        if method2:
            return method2
        if normalized_known_name:
            method3 = self._cascade.detect_via_external_roots(
                space_id,
                external_game_roots,
                normalized_known_name,
                known_name,
                check_install_state,
            )
            if method3:
                return method3
        return self._cascade.detect_via_registry_install_id(
            space_id,
            prefix_path,
            known_name,
            check_install_state,
        )

    def _get_game_name(self, space_id: str) -> str | None:
        """Get game name."""
        entry = self._id_map.get_entry(space_id)
        return entry.get("name")
