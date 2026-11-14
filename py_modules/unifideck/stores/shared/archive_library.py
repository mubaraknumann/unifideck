"""Overlay local archive installs without changing chosen launch targets."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.core.types import Game

from .archive_install import ArchiveInstaller
from .archive_source import CatalogSource
from .install_status import merge_install_status

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
        # Off the event loop: the marker read and the per-install ``is_dir``
        # can both land on a sleeping SD card.
        installed = await asyncio.to_thread(self._installer.get_installed)
        # A marker can outlive its directory (a manual ``rm``, a wiped SD
        # card), so the shared overlay re-checks every path against disk.
        # Without that a game with no files reads as installed: Steam shows
        # PLAY and the QAM Installed list shows a row with no size.
        #
        # ``exe_key`` is deliberately left unset, so no exe is carried. The
        # launch target is written once, at install time, and after that it
        # belongs to the user: an archive contains whatever its owner
        # uploaded, so the auto-detected executable is a guess more often
        # than for any other store, and Change Executable is the fix.
        # Reconcile only overwrites a games.map row when the synced game
        # carries an exe, so by carrying none a sync can never revert that
        # choice. (Epic and Amazon behave the same way; GOG carries one
        # because it must discover installs it did not perform.)
        games = await asyncio.to_thread(merge_install_status, games, installed)
        _log_missing_installs(games, installed)
        logger.info("[ArchiveLibrary] %d game(s) resolved", len(games))
        return games


def _log_missing_installs(
    games: list[Game], installed: dict[str, dict[str, Any]],
) -> None:
    """Say which install markers were ignored because their folder is gone.

    The marker is kept, never deleted: if the folder comes back (the SD
    card is re-inserted), the next sync reads the game as installed again.
    """
    for game in games:
        marker = installed.get(game.store_game_id)
        if marker and marker.get("install_path") and not game.installed:
            logger.info(
                "[ArchiveLibrary] %s %s (%s) has an install marker but its "
                "folder %s is missing; showing it as not installed",
                game.store, game.title, game.store_game_id,
                marker.get("install_path"),
            )
