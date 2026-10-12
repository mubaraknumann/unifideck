"""Overlay local archive installs without changing chosen launch targets."""
from __future__ import annotations

import logging

from unifideck.core.types import Game

from .archive_install import ArchiveInstaller
from .archive_source import CatalogSource

logger = logging.getLogger(__name__)


class ArchiveLibraryReader:
    """Turns a catalog into a library, with install state overlaid."""

    def __init__(
        self, installer: ArchiveInstaller, catalog: CatalogSource,
    ) -> None:
        self._installer = installer
        self._catalog = catalog

    async def get_library(self, *, force: bool = False) -> list[Game]:
        """Every owned game, or raise if the catalog could not be read.

        **Never returns a short list on failure.** A store that answers a
        sync with fewer games than the user has is indistinguishable from
        one whose library shrank, and the shortcut reconcile believes it:
        the missing games' shortcuts get swept. A catalog that cannot answer
        therefore raises, and the store's ``get_library`` turns that into
        ``None`` — the documented "could not answer" signal that leaves the
        existing shortcuts alone.

        This matters for every source, for the same reason: a remote server is
        offline routinely, and a local vault may sit on an SD card that has not
        mounted yet.
        """
        del force  # every catalog fetches fresh; accepted for interface parity
        games = await self._catalog.fetch()
        for game in games:
            install_info = self._installer.get_install_info(game.store_game_id)
            if install_info:
                game.installed = True
                game.install_path = install_info.get("install_path")
                # ``exe_path`` is deliberately NOT carried. The launch target
                # is written once, at install time, and after that it belongs
                # to the user: an archive contains whatever its owner
                # uploaded, so the auto-detected executable is a guess more
                # often than for any other store, and Change Executable is
                # the fix. Reconcile only overwrites a games.map row when the
                # synced game carries an exe — so by carrying none, a sync can
                # never revert that choice. (Epic and Amazon behave the same
                # way; GOG carries one because it must discover installs it
                # did not perform.)
        logger.info("[ArchiveLibrary] %d game(s) resolved", len(games))
        return games
