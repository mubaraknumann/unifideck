"""
Ubisoft library facade — orchestrates fetch, detect, build, filter.

``UbisoftLibrary`` is the public entry-point of the library sub-package.
It composes the work of:

* ``fetch.py`` — pull the UPC owned-games catalog;
* ``data_loader.py`` — load installed-state from disk markers;
* ``detection.py`` — detect installs the catalog doesn't know about;
* ``manifest.py`` — produce display-ready ``GameRecord`` entries.

(``steam_filter.py`` — Steam dedup — was removed in
commits 6c84e7e / 908d350; the filter is currently a no-op
pending a fixed implementation.)

The result is the merged list of owned + installed Ubisoft games that
the UI displays. Cached in-memory by the store and invalidated on:
auth state change, install/uninstall, manual user refresh.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.core.types import Game
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths

from .detection import _InstallDetector
from .fetch import _LibraryFetcher
from .free_to_play import _FreeToPlayFeed
from .manifest import _VisibleManifestProcessor

logger = logging.getLogger(__name__)

class UbisoftLibrary:
    """Ubisoft library."""

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        id_map: UbisoftIdMap,
        queue_template_creation: Callable[[], None],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._id_map = id_map
        self._queue_template_creation = queue_template_creation
        self._detector = _InstallDetector(
            config=config,
            id_map=id_map,
        )
        self._fetcher = _LibraryFetcher(
            config=config,
            paths=paths,
            id_map=id_map,
        )
        self._manifest = _VisibleManifestProcessor(
            config=config,
            id_map=id_map,
            load_json_file_safe=self._detector.load_json_file_safe,
        )
        self._free_to_play = _FreeToPlayFeed(config=config)

    async def get_library(self, *, force: bool = False) -> list[Game]:
        """Get library (``force`` re-pulls the unifiDB lookups, bypassing TTL)."""
        try:
            installed = await self._detector.get_installed()
            local_games = await self._fetcher.fetch_local_binaries(
                installed,
                force=force,
            )
            if local_games is None:
                logger.info(
                    "[UbisoftLibrary] no local binary data available yet",
                )
                return []
            logger.info(
                "[UbisoftLibrary] library: %d games from local binaries",
                len(local_games),
            )
            override_manifest = self._manifest.load_manifest()
            if override_manifest:
                local_games = self._manifest.apply_filter(
                    local_games,
                    installed,
                    override_manifest,
                    source_label="override",
                )
            if self._config.enable_free_to_play_feed:
                ftp_entries = await self._free_to_play.fetch_entries()
                local_games = self._manifest.supplement(
                    local_games,
                    installed,
                    ftp_entries,
                    source_label="free-to-play",
                )
            if local_games:
                template_dir = self._config.template_dir_expanded
                template_marker = str(Path(template_dir) / self._config.bootstrap_marker)
                if not await asyncio.to_thread(lambda: Path(template_marker).is_file()):
                    self._queue_template_creation()
            return local_games
        except Exception:
            logger.exception("[UbisoftLibrary] error fetching library")
            return []

    async def get_installed(self) -> dict[str, Any]:
        """Get installed."""
        return await self._detector.get_installed()

    def get_installed_game_info(
        self,
        game_id: str,
    ) -> dict[str, Any] | None:
        """Get installed game info."""
        return self._detector.get_installed_game_info(game_id)

    def find_game_executable(
        self,
        install_path: str,
    ) -> str | None:
        """Find game executable."""
        return self._detector.find_game_executable(install_path)

    async def write_install_marker(
        self,
        space_id: str,
        install_path: str,
        executable: str,
        game_title: str = "",
    ) -> None:
        """Write install marker."""
        await self._detector.write_install_marker(
            space_id=space_id,
            install_path=install_path,
            executable=executable,
            game_title=game_title,
        )

    @staticmethod
    def get_game_official_url(game_id: str) -> str:
        """Get game official URL."""
        return _InstallDetector.get_game_official_url(game_id)
