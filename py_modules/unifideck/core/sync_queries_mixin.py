"""Read-only query mixin for :class:`SyncService`.

Extracted from ``core/sync_service.py`` (2026-05-14) to keep
the host file under the 550-LOC volumetry cap. The split is
clean along the read/write axis :

* **Writes** stay in ``SyncService`` proper — sync orchestration,
  cancellation, event emission, cache management.
* **Reads** (this file) — pure snapshot accessors over
  ``_all_games``: status dict, full library, per-store list,
  per-AppID lookup, plus the ``_flatten`` static helper.

This mirrors the mixin pattern used elsewhere in the project
(``_VdfShortcutsMixin``, ``_FailuresMixin``, ``_GamesMapMixin``)
so the import surface — ``from unifideck.core.sync_service
import SyncService`` — stays unchanged.

The mixin declares its consumed attributes (``_all_games``,
``_lock``, ``_current_store``, ``_last_sync_time``) as
``TYPE_CHECKING`` annotations only — they are provided by the
host ``SyncService`` at runtime, the same convention the other
mixins use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .compat_bridge import appid_candidates
from .types import Game

if TYPE_CHECKING:
    import asyncio

class _SyncQueriesMixin:
    """Read-only snapshot accessors for :class:`SyncService`.

    Every method here is a non-mutating view over
    ``_all_games`` and the timing/cursor fields. Safe to call
    concurrently with a running sync — Python's GIL gives us
    atomic dict-get semantics, and the lists we return are
    shallow copies so the caller can iterate without fear.
    """

    # Attributes provided by the host SyncService at runtime.
    # Declared here so type-checkers don't complain about the
    # mixin reading state it didn't set itself.
    _all_games: dict[str, list[Game]]
    # ``_last_sync_time`` is Optional: None marks "no sync has ever
    # run yet" (fresh install / post-reset) and the frontend status
    # poller passes it through to the UI as null. The host SyncService
    # initialises it to None and overwrites with time.time() once
    # the first sync completes. Lot 12d fix: was typed as ``float``
    # here, which forced the host to declare its attribute as plain
    # ``float`` too — but the runtime contract is Optional, so mypy
    # strict flagged the host's ``float | None`` initialisation as
    # incompatible-assignment against the mixin's claim.
    _last_sync_time: float | None
    _current_store: str | None
    _lock: asyncio.Lock
    # The per-sync-run progress tracker (owned by SyncService).
    # ``get_status`` delegates its ``to_dict()`` directly.
    _progress: Any  # :class:`unifideck.core.sync_progress.SyncProgress`

    def get_status(self) -> dict[str, Any]:
        """Return the ``SyncProgress.to_dict()`` enriched with status fields.

        The ``syncing`` flag is derived from the progress tracker's
        ``status``: any phase other than ``complete`` / ``error`` /
        ``cancelled`` / ``idle`` is considered in-flight. This
        keeps the frontend's 500ms polling loop alive through
        post-sync enrichment.
        """
        progress = getattr(self, "_progress", None)
        if progress is None:
            return {
                "syncing": False,
                "last_sync_time": self._last_sync_time,
            }
        result: dict[str, Any] = progress.to_dict()
        in_flight = result.get("status") not in (
            "complete", "error", "cancelled", "idle",
        )
        result["syncing"] = in_flight or self._lock.locked()
        result["current_store"] = self._current_store
        result["last_sync_time"] = self._last_sync_time
        # read the cooldown from the host; fall back to 5 seconds
        cooldown = getattr(self, "_cooldown_ms", 5000)
        result["cooldown_ms"] = cooldown if isinstance(cooldown, int) else 5000
        return result

    def get_all_games(self) -> list[Game]:
        """Return the merged unified library (flattened across stores).

        Snapshot copy via ``_flatten`` — caller can
        iterate without worrying about ``_all_games``
        being mutated by a concurrent sync.

        Returns:
            List of ``Game`` instances.
        """
        return self._flatten(self._all_games)

    def get_games_by_store(self, store: str) -> list[Game]:
        """Return the games list for one store (shallow copy).

        Empty list when the store has no games or
        doesn't exist — callers don't need to handle a
        ``None`` return.

        Args:
            store: store identifier.

        Returns:
            List of games (shallow copy).
        """
        return list(self._all_games.get(store, []))

    def get_game_info(self, app_id: int) -> dict[str, Any] | None:
        """Find a game by AppID and return its dict form.

        Linear scan across every store's list — O(N) on
        total library size. Acceptable because callers
        are interactive (single game per call from RPC),
        not bulk loops.

        Accepts either the signed or unsigned 32-bit form of the
        shortcut AppID. The sync layer stores
        :attr:`Game.app_id` as signed (matches how Steam
        serialises it — see ``services/shortcut/games_map.py``)
        but Steam's frontend hands plugins the unsigned form via
        ``overview.appid``, so the RPC's wire value is whichever
        form the caller has on hand. Normalising here means
        callers don't have to know the convention.

        ``dataclasses.asdict`` is imported lazily inside
        the hit branch to keep the cold path zero-cost
        (no import unless something matches).

        Args:
            app_id: Steam-style AppID (signed or unsigned).

        Returns:
            Dict form of the game, or ``None`` if not
            found.
        """
        # Both representations of the same 32-bit integer. Shared with the
        # two other former copies of this arithmetic via compat_bridge; ints
        # here because the comparison below is against ``Game.app_id``.
        candidates = {int(form) for form in appid_candidates(app_id)}
        for games in self._all_games.values():
            for game in games:
                if game.app_id in candidates:
                    from dataclasses import asdict

                    return asdict(game)
        return None

    @staticmethod
    def _flatten(libraries: dict[str, list[Game]]) -> list[Game]:
        """Merge per-store lists into one flat list.

        Order: dict-iteration order over stores (insertion
        order on CPython 3.7+), then per-store insertion
        order. Stable across calls so the UI's game
        ordering doesn't shuffle between syncs.

        Args:
            libraries: per-store mapping.

        Returns:
            Single flat list of games.
        """
        merged: list[Game] = []
        for games in libraries.values():
            merged.extend(games)
        return merged
