"""Per-store sync mixin for :class:`SyncService`.

The Quick Access store rows drive a sync for one store at a time — "Sync
games" and "Sync images" on a single row — and show each store's own
status. This mixin owns that surface:

* the two public entry points, :meth:`sync_stores` and
  :meth:`resync_artwork`, which queue a scoped request through the same
  single-flight queue as every other sync;
* the scope helpers the run loop calls (which available stores a run
  covers, and which library it hands to finalize);
* the per-store summary ``get_status`` reports while nothing is running
  (game count + when the store last synced) and the queued scope.

Scoping rules themselves live in ``core/sync_scope.py``. Split out of
``sync_service.py`` / ``sync_run_mixin.py`` for the 550-LOC cap; all
consumed state is declared as ``TYPE_CHECKING`` annotations, provided by
the host at runtime.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .sync_availability import refresh_store_availability
from .sync_scope import merge_scoped_libraries, normalize_stores
from .types import Game, SyncRequest, SyncResult

if TYPE_CHECKING:
    from unifideck.stores import StoreRegistry
    from unifideck.stores.shared.store_base import StoreBase

logger = logging.getLogger(__name__)


class _SyncScopeMixin:
    """Per-store sync entry points and status for :class:`SyncService`."""

    # State provided by the host SyncService at runtime.
    _registry: StoreRegistry
    _all_games: dict[str, list[Game]]
    _last_sync_time: float | None
    _store_sync_times: dict[str, float]
    _pending_request: SyncRequest | None
    _request_lock: asyncio.Lock

    if TYPE_CHECKING:
        async def _enqueue(self, request: SyncRequest) -> SyncResult: ...

    async def sync_stores(
        self, stores: Iterable[Any] | None = None, *, source: str = "manual",
    ) -> SyncResult:
        """Re-fetch the libraries of ``stores`` (``None`` = every store).

        A force fetch — each store bypasses its own library cache — since
        the user asked for it by name. Queued behind any in-flight sync,
        so a second row's request shows as queued rather than being
        refused.
        """
        return await self._enqueue(SyncRequest(
            kind="force", source=source, stores=normalize_stores(stores),
        ))

    async def resync_artwork(
        self, stores: Iterable[Any] | None = None, *, source: str = "manual",
    ) -> SyncResult:
        """Re-download artwork for ``stores``' games (``None`` = every store).

        Fetches no library: the games come from the last sync, and only
        the artwork phase does work.
        """
        return await self._enqueue(SyncRequest(
            kind="artwork", source=source, resync_artwork=True,
            stores=normalize_stores(stores),
        ))

    async def _available_in_scope(
        self, stores: frozenset[str] | None,
    ) -> list[StoreBase]:
        """Refresh availability and return the available stores in scope."""
        await refresh_store_availability(self._registry)
        available_stores = [
            s for s in self._registry.available()
            if stores is None or s.store_name in stores
        ]
        store_names = {s.store_name for s in available_stores}
        # Surface stores excluded from this sync. A dropped store never
        # reaches its per-store "fetched N games" log, so without this a
        # silently-skipped store (e.g. GOG after a transient availability
        # probe blip) looks identical to "0 games" in an all-green log
        # (UD-005).
        dropped = [
            s.store_name
            for s in self._registry.all()
            if s.store_name not in store_names
            and (stores is None or s.store_name in stores)
        ]
        if dropped:
            logger.warning(
                "[SyncService] stores excluded from sync "
                "(not available): %s",
                dropped,
            )
        return available_stores

    def _library_for_run(
        self,
        fetched: dict[str, list[Game]],
        stores: frozenset[str] | None,
        artwork_only: bool,
    ) -> dict[str, list[Game]]:
        """The whole library this run hands to finalize.

        A full sync replaces the library with what it fetched, exactly
        as before scoping existed. A scoped or artwork-only run keeps
        every store it did not fetch at its last-known library.
        """
        if stores is None and not artwork_only:
            return fetched
        return merge_scoped_libraries(self._all_games, fetched)

    def _record_store_sync_times(
        self, fetched: Iterable[str], errors: dict[str, str],
    ) -> None:
        """Stamp every store that this run fetched cleanly."""
        now = time.time()
        for name in fetched:
            if name not in errors:
                self._store_sync_times[name] = now

    def store_summary(self) -> dict[str, dict[str, Any]]:
        """Per-store ``{count, synced_at}`` for the idle store rows.

        ``synced_at`` falls back to the last whole-library sync for a
        store that has no stamp of its own (a cache written before
        per-store stamps existed).
        """
        return {
            name: {
                "count": len(games),
                "synced_at": self._store_sync_times.get(
                    name, self._last_sync_time,
                ),
            }
            for name, games in self._all_games.items()
        }

    async def _take_pending(self) -> SyncRequest | None:
        """Remove and return the queued request (``None`` if cancel took it)."""
        async with self._request_lock:
            current, self._pending_request = self._pending_request, None
        return current

    def queued_scope(self) -> dict[str, Any] | None:
        """The request waiting behind the in-flight sync, if any.

        ``stores`` is ``None`` when the queued request covers every
        store, so the frontend can mark every row as queued.
        """
        pending = self._pending_request
        if pending is None:
            return None
        return {
            "kind": pending.kind,
            "stores": sorted(pending.stores) if pending.stores is not None else None,
        }
