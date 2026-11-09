"""The Microsoft store's read-only Xbox PC save status.

A mixin so ``microsoft_store.py`` stays under the file-size cap. The comparing
is in ``xodus/save_status.py``. This adds the install gate, the token and a
short cache, because the game page asks every time it opens.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .xodus import save_status
from .xodus.ids import record_for_game

if TYPE_CHECKING:
    from .tokens import MicrosoftTokenManager
    from .tokens.xbl_chain import XBLTokenChain
    from .xodus.xbox_pc import XboxPc

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 60.0
NOT_APPLICABLE: dict[str, Any] = {"applicable": False, "state": "", "waiting": 0, "checked_at": 0.0}


class MicrosoftSaveStatusMixin:
    """Xbox PC save status for :class:`MicrosoftStore`."""

    _tokens: MicrosoftTokenManager
    _xbox_pc: XboxPc
    _save_status_cache: dict[str, tuple[float, dict[str, Any]]] | None = None
    _save_status_locks: dict[str, asyncio.Lock] | None = None

    async def _save_chain(self) -> XBLTokenChain | None:
        """The ``xboxlive.com`` chain with an xuid, or None."""
        chain = await self._tokens.build_chain()
        if chain is None or not chain.xuid:
            # The ticket may have just expired: renew it once.
            await self._tokens.refresh_if_stale()
            chain = await self._tokens.build_chain()
        return chain if chain is not None and chain.xuid else None

    async def get_xbox_save_status(self, game_id: str, force: bool = False) -> dict[str, Any]:
        """Compare an installed Xbox PC game's saves with the cloud. Never writes.

        ``force`` skips the cache, for a game that has just stopped.
        """
        if not self._xbox_pc.enabled():
            return dict(NOT_APPLICABLE)
        record = await asyncio.to_thread(record_for_game, game_id)
        if record is None or record.get("state") != "installed":
            return dict(NOT_APPLICABLE)
        pid = str(record["productId"])
        if await asyncio.to_thread(save_status.game_is_running, pid):
            logger.info("[XboxSaves] %s: %s", pid, save_status.RUNNING)
            return {"applicable": True, "state": save_status.RUNNING, "waiting": 0, "checked_at": time.time()}
        if self._save_status_cache is None or self._save_status_locks is None:
            self._save_status_cache, self._save_status_locks = {}, {}
        # One check at a time per game: the page can ask twice while it opens.
        async with self._save_status_locks.setdefault(pid, asyncio.Lock()):
            cached = self._save_status_cache.get(pid)
            if cached is not None and not force and time.monotonic() - cached[0] < _CACHE_SECONDS:
                return cached[1]
            chain = await self._save_chain()
            result = await asyncio.to_thread(
                save_status.check_saves, chain, pid, Path(str(record.get("gameDir", ""))),
            )
            # The UI folds most states into one line, so the real one is kept here.
            logger.info("[XboxSaves] %s: %s, %d waiting", pid, result["state"], result["waiting"])
            self._save_status_cache[pid] = (time.monotonic(), result)
            return result

    async def count_unuploaded_saves(self, game_id: str) -> int:
        """Saves that deleting the game's prefix would lose. Local files only."""
        record = await asyncio.to_thread(record_for_game, game_id)
        if record is None:
            return 0
        return await asyncio.to_thread(save_status.count_not_uploaded, str(record["productId"]))
