"""The owned EA library, read through ``maxima-cli list-games --json``.

Maxima already keeps only downloadable full games and folds editions onto their
base game. Every entry is shown, whatever the ownership: EA serves the build and
the licence for games bought on Steam or Epic too (unlike Epic and Ubisoft
titles owned on Steam, which only run when Steam launches them), and Game Pass
(``XGP_VAULT``) entries are licensed by EA. ``metadata["ownership"]`` keeps how
the account owns each one; game grouping pairs an EA copy with a Steam copy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from unifideck.core.types import Game
from unifideck.launcher.ea.records import all_records
from unifideck.stores.shared.install_status import merge_install_status

from .cli import MaximaCli, MaximaCliError
from .ids import STORE, row_id

logger = logging.getLogger(__name__)

def game_from_entry(entry: dict[str, Any]) -> Game | None:
    """A library row for one ``list-games`` entry, or None when it is unusable."""
    offer = entry.get("offer_id")
    if not isinstance(offer, str) or not offer:
        return None
    return Game(
        app_id=0,
        store=STORE,
        store_game_id=row_id(offer),
        title=str(entry.get("name") or entry.get("slug") or offer),
        metadata={
            "offer_id": offer,
            "content_id": entry.get("content_id"),
            "slug": entry.get("slug"),
            "has_cloud_save": bool(entry.get("has_cloud_save")),
            "ownership": entry.get("ownership") or [],
        },
    )


class EALibraryReader:
    """Reads the owned library; ``None`` whenever it cannot be read."""

    def __init__(self, cli: MaximaCli) -> None:
        self._cli = cli
        #: The last ``list-games`` entries, by row id (installs need the offer).
        self._entries: dict[str, dict[str, Any]] = {}

    async def entry(self, game_id: str) -> dict[str, Any] | None:
        """The ``list-games`` entry for ``game_id``, reading the library once if needed."""
        if game_id not in self._entries:
            await self.get_library()
        return self._entries.get(game_id)

    async def get_library(self) -> list[Game] | None:
        """Owned EA games, or None when the library could not be read.

        Never ``[]`` on failure: an empty list would remove every EA shortcut.
        """
        try:
            entries = await self._cli.list_games()
        except MaximaCliError as e:
            logger.warning("[EA] could not read the library: %s", e)
            return None
        pairs = [(game, entry) for entry in entries if (game := game_from_entry(entry)) is not None]
        self._entries = {game.store_game_id: entry for game, entry in pairs}
        games = [game for game, _ in pairs]
        logger.info("[EA] library: %d game(s), %d listed by EA", len(games), len(entries))
        records = await asyncio.to_thread(all_records)
        return merge_install_status(games, {gid: asdict(r) for gid, r in records.items()}, exe_key="exe")
