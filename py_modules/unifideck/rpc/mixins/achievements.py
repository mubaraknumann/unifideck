"""AchievementsRPCMixin — game achievements (display) + last-session summary.

Two RPCs powering the App-Details achievements UI:

* ``get_game_achievements`` — the full list + this user's unlock status, read
  live from the store backend (GOG's ``gameplay.gog.com``). May hit the network
  / refresh tokens, so it's called on demand (modal open / manual refresh), not
  on the render hot path. A game with no achievements is a normal empty payload.
* ``get_last_session_achievements`` — a fast, network-free read of the
  achievement watcher's persisted ``last_session`` summary, for the game-info
  panel's "last session" row (keeps ``get_game_info`` non-blocking).

Store-generic on purpose (the ``store`` arg + the ``achievements_unsupported``
branch) so Epic — which unlocks/notifies via its own EOS overlay today — can be
added later with no frontend or RPC-name change. GOG is the only impl for now.
"""
from __future__ import annotations

from typing import Any

from unifideck.core.store_capabilities import ACHIEVEMENT_STORES
from unifideck.rpc import RpcError
from unifideck.stores.shared.achievements_error import StoreAchievementsError

# Single source of truth, shared with the ``get_store_infos`` payload the
# frontend reads. This used to be a local tuple whose exact twin lived in
# ``GameInfoCompatRow.tsx`` with nothing linking them (audit register 31):
# a store added here alone shows no button, and one added there alone shows a
# button that raises ``achievements_unsupported``.
_ACHIEVEMENT_STORES = tuple(sorted(ACHIEVEMENT_STORES))


class AchievementsRPCMixin:
    """Achievements RPC surface (GOG today; store-generic for later)."""

    registry: Any
    services: Any

    async def get_game_achievements(
        self, store: str, game_id: str, force: bool = False,
    ) -> Any:
        """A game's achievements (definitions + this user's unlock status).

        ``force`` bypasses the store's TTL cache (manual refresh). Raises a
        typed ``RpcError`` (``offline`` / ``auth_expired`` / ``not_authed`` /
        ``no_client_id`` / ``achievements_unsupported``) the frontend switches
        on; ``total == 0`` is success, not an error.
        """
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        if store not in _ACHIEVEMENT_STORES:
            raise RpcError("achievements_unsupported", store=store)
        inst = self.registry.get_store(store) if self.registry else None
        if inst is None:
            raise RpcError("store_unavailable", store=store)
        if not await inst.is_available():
            raise RpcError("not_authed", store=store)
        try:
            return await inst.get_game_achievements(game_id, force=force)
        # Base class, not a tuple of the two concrete ones: a third
        # store's achievements error is then handled the day it is
        # written, instead of escaping unhandled for that store alone.
        except StoreAchievementsError as e:
            raise RpcError(e.code, **e.context) from e

    async def get_last_session_achievements(
        self, store: str, game_id: str,
    ) -> Any:
        """Last play session's unlock summary, or None (fast, local-only).

        Reads the watcher's persisted state — no network, no token exchange —
        so the game-info panel can show "Last session: …" without blocking.
        Returns ``{names, unlocked, total, at}`` or None when there's no
        recorded session.
        """
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        watcher = getattr(self.services, "achievements", None)
        if watcher is None:
            return None
        return await watcher.get_last_session(store, game_id)
