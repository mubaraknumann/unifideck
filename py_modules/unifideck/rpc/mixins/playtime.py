"""Playtime RPC mixin for Plugin class.
"""
from __future__ import annotations

from typing import Any

from unifideck.rpc.errors import RpcError


class PlaytimeRPCMixin:
    """Per-game and aggregate playtime queries."""

    services: Any

    def _require_playtime(self) -> Any:
        """Return PlaytimeService or raise ``service_unavailable``."""
        svc = getattr(self.services, "playtime", None)
        if svc is None:
            raise RpcError("service_unavailable", service="playtime")
        return svc

    async def get_playtime(self, store: str, game_id: str) -> Any:
        """Return playtime data for a specific game.

        Real method is :meth:`PlaytimeService.get_playtime` (see
        handler twin for the rationale).

        The only playtime route. Two siblings were removed as dead
        (audit §1.2): ``get_all_playtimes``, because the library view
        sources bulk playtime from Steam's own ``GetPlaytime`` rather
        than from us, and ``sync_playtime_now``, because the drain
        already runs at startup and on every ``PLAYTIME_UPDATED``,
        with unreported sessions persisted in the DB until they land.
        """
        return await self._require_playtime().get_playtime(store, game_id)
