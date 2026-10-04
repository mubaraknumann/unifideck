"""MicrosoftOwnershipService: the Xbox purchase list behind the Steam Store ribbon.

Keeps an index of every product the Microsoft account owns (read by
``MicrosoftStore.get_owned_products``), maps each product to its Steam
AppID, and hands the ribbon an in-memory view. It never creates library
rows or shortcuts, and never touches ``get_library``: an ownership outage
can only cost the ribbon its "Owned on Xbox" line, never a game tile.

When it refreshes:

- after a sync, once the ``proton_meta`` phase finishes (the last user of
  Steam's storesearch), if the list is older than :data:`STALE_SECONDS`;
  it always maps products still missing a Steam AppID;
- right after a Microsoft sign-in (forced);
- when the ribbon asks and the list is stale (``nudge``).

Failure keeps the last good list. So does a successful answer with no
products when the previous list had some: losing every purchase at once is
far likelier to be a Microsoft-side fault than a real change.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe

from .index import clear_index, load_index, merge, save_index, to_view
from .steam_resolve import resolve_pending, seed_from_library

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.core.cross_store_ownership import PurchaseIndex
    from unifideck.core.types.domain import Game
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.ownership import OwnedFetch
    from unifideck.stores.shared.store_registry import StoreRegistry

logger = logging.getLogger(__name__)

STALE_SECONDS = 12 * 3600
RETRY_SECONDS = 30 * 60


class MicrosoftOwnershipService:
    """Xbox purchase index for the Steam Store ownership ribbon."""

    def __init__(
        self,
        bus: EventBus,
        registry: StoreRegistry,
        cache: CacheManager,
        config: ConfigManager | None = None,
    ) -> None:
        """Initialize the instance (loads the stored index; no network)."""
        self._bus = bus
        self._registry = registry
        self._cache = cache
        self._config = config
        self._index = load_index(cache)
        self._view: PurchaseIndex | None = to_view(self._index) if self._index else None
        self._games: list[Game] = []
        self._task: asyncio.Task[None] | None = None
        self._retry_after = 0.0
        auto_wire(self, bus)

    # ── what the ribbon reads ────────────────────────────────────────

    def purchase_indexes(self) -> dict[str, PurchaseIndex]:
        """The indexes for ``find_owned_copies``; ``{}`` before the first read."""
        return {"microsoft": self._view} if self._view is not None else {}

    def nudge(self) -> None:
        """Refresh in the background if the list is stale. Never raises."""
        if self._busy() or time.time() < self._retry_after or not self._stale():
            return
        self._schedule(force=True)

    # ── events ───────────────────────────────────────────────────────

    @subscribe(Events.POST_SYNC_PHASE_CHANGED)
    async def _on_post_sync_phase(self, **kwargs: Any) -> None:
        """Remember the sync's games; refresh once Steam's search is idle."""
        if kwargs.get("active") is not False:
            return
        phase = kwargs.get("phase")
        if phase == "artwork":
            games = (kwargs.get("sync_kwargs") or {}).get("games")
            if isinstance(games, list):
                self._games = games
        elif phase == "proton_meta" and not self._busy():
            self._schedule(force=False)

    @subscribe(Events.STORE_AUTH_COMPLETE)
    async def _on_auth_complete(self, **kwargs: Any) -> None:
        """A new Microsoft sign-in: read its purchases now."""
        if kwargs.get("store") != "microsoft":
            return
        self._retry_after = 0.0
        await self._cancel()
        self._schedule(force=True)

    @subscribe(Events.STORE_LOGOUT)
    async def _on_logout(self, **kwargs: Any) -> None:
        """Signed out: the purchases are no longer the user's to show."""
        if kwargs.get("store") != "microsoft":
            return
        await self._cancel()
        clear_index(self._cache)
        self._index = None
        self._view = None

    async def stop(self) -> None:
        """Teardown: stop any refresh in flight."""
        await self._cancel()

    # ── the refresh ──────────────────────────────────────────────────

    def _schedule(self, *, force: bool) -> None:
        self._task = asyncio.create_task(self._refresh(force=force), name="microsoft-ownership")

    async def _refresh(self, *, force: bool) -> None:
        try:
            if force or self._stale():
                await self._fetch_and_commit()
            if self._index is not None:
                await self._map_to_steam()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[MicrosoftOwnership] refresh failed")
            self._retry_after = time.time() + RETRY_SECONDS

    async def _fetch_and_commit(self) -> None:
        store = self._microsoft_store()
        if store is None:
            return
        fetch: OwnedFetch = await store.get_owned_products()
        if not fetch.ok:
            self._keep_last_good(fetch.reason)
            return
        before = len((self._index or {}).get("products") or {})
        if not fetch.products and before:
            self._keep_last_good("Microsoft returned no purchases at all")
            return
        self._commit(merge(self._index, fetch, time.time()))
        logger.info(
            "[MicrosoftOwnership] %d owned Xbox products (%s)",
            len(fetch.products), ", ".join(f"{k}={v}" for k, v in sorted(fetch.stats.items())),
        )

    async def _map_to_steam(self) -> None:
        index = self._index
        if index is None:
            return
        seeded = seed_from_library(index, self._games, self._cache)
        matched, missed = await resolve_pending(index, self._config, save=lambda: self._commit(index))
        self._commit(index)
        if seeded or matched or missed:
            logger.info(
                "[MicrosoftOwnership] Steam matches: %d from the library, %d found by "
                "search, %d not on Steam", seeded, matched, missed,
            )

    def _keep_last_good(self, reason: str) -> None:
        """Rule 8: say what the user loses, and where we looked."""
        self._retry_after = time.time() + RETRY_SECONDS
        if self._index is None:
            logger.warning(
                "[MicrosoftOwnership] could not read Xbox purchases (%s). Steam Store "
                "pages show Xbox titles only as CLOUD, never OWNED; retrying in %d min.",
                reason, RETRY_SECONDS // 60,
            )
            return
        fetched = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._index.get("fetched_at", 0)))
        logger.warning(
            "[MicrosoftOwnership] could not read Xbox purchases (%s); keeping the list "
            "from %s (%d products). Purchases made since then are not marked OWNED on "
            "Steam Store pages; retrying in %d min.",
            reason, fetched, len(self._index.get("products") or {}), RETRY_SECONDS // 60,
        )

    def _commit(self, index: dict[str, Any]) -> None:
        save_index(self._cache, index)
        self._index = index
        self._view = to_view(index)

    def _stale(self) -> bool:
        fetched = (self._index or {}).get("fetched_at") or 0
        return time.time() - fetched > STALE_SECONDS

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _microsoft_store(self) -> Any:
        try:
            return self._registry.get("microsoft")
        except KeyError:
            return None
