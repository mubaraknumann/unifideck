"""Sync RPC mixin for Plugin class.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from unifideck.rpc.mixins.sync_cleanup import CleanupRPCMixin
from unifideck.services.installed_disk_info import collect_installed_disk_info
from unifideck.services.size_cache import get_size_cache
from unifideck.stores.shared.installed_size import installed_size_bytes

logger = logging.getLogger(__name__)

# Upper bound for a single not-installed download-size lookup
# (``store.get_game_size`` shells out to legendary / gogdl). Keeps the
# ``get_game_size_bytes`` RPC from hanging on a slow or offline store.
_SIZE_LOOKUP_TIMEOUT_S = 30.0

class SyncRPCMixin(CleanupRPCMixin):
    """Library sync, progress, and game queries.

    The "Delete all Unifideck data" flow lives in
    :class:`~unifideck.rpc.mixins.sync_cleanup.CleanupRPCMixin`, mixed in here
    to keep this file under the volumetry cap.

    Reclaiming Steam-made ``compatdata`` prefixes used to be an RPC pair here,
    driven by a Settings button. It now runs unattended at boot from
    ``services/prefix_bridge.reclaim_redundant_compatdata`` and has no RPC
    surface, so there is nothing for the frontend to call.
    """

    sync_service: Any
    config: Any

    async def sync_libraries(
        self, fetch_artwork: bool = True, **kw: Any,
    ) -> Any:
        """Trigger a full library sync across every store.

        Args:
            fetch_artwork: when ``False``, skip the artwork
                download phase entirely. Used by background /
                scheduled syncs that only need a fresh game list.

        The underlying service method is ``sync_all`` (an earlier
        version called ``sync`` which doesn't exist on
        :class:`SyncService` — the RPC raised ``AttributeError``).
        """
        return await self.sync_service.sync_all(
            fetch_artwork=fetch_artwork, **kw,
        )

    async def force_sync_libraries(
        self, resync_artwork: bool = False, **kw: Any,
    ) -> Any:
        """Like ``sync_libraries`` but bypasses per-store cache TTLs.

        Used for "force refresh" — when the cache hasn't
        expired but the library is known to have changed.

        Args:
            resync_artwork: when ``True``, ArtworkService clears
                its SGDB failure-cooldown cache and bypasses the
                ``has_artwork`` on-disk skip so every game gets a
                fresh download. Wired end-to-end via the
                SYNC_COMPLETE event payload.
            **kw: forwarded with ``force=True`` added.

        Returns:
            Sync-outcome dict.
        """
        return await self.sync_service.sync_all(
            force=True, resync_artwork=resync_artwork, **kw,
        )

    async def get_sync_progress(self) -> Any:
        """Return the full sync snapshot: in-flight progress + status.

        Progress, the ``syncing`` flag and ``cooldown_ms`` are all
        bundled into ``get_status`` — there is no separate
        ``get_progress`` on :class:`SyncService`. This is the only
        route onto that snapshot; a ``get_sync_status`` alias that
        returned the identical call was removed as dead (audit §1.2).
        """
        return self.sync_service.get_status()

    async def cancel_sync(self) -> Any:
        """Cancel an in-flight sync."""
        return await self.sync_service.cancel()

    async def request_auth_sync(self, store: str) -> Any:
        """Frontend-callable trigger for post-login refresh.

        Called by AuthDispatcher after a store login completes
        (e.g. Ubisoft, which has no browser-callback auth). If a
        sync is already running, the request is queued behind it
        via ``SyncService._enqueue``; the response carries
        ``restart_pending=True`` so the frontend knows to re-listen
        for ``SYNC_STARTED``.
        """
        return await self.sync_service.request_auth_sync(store)

    async def get_all_unifideck_games(self) -> Any:
        """Return every known game across every store.

        :meth:`SyncService.get_all_games` is synchronous; an
        earlier version awaited it and crashed with
        ``TypeError: object list can't be used in 'await' expression``.
        """
        return self.sync_service.get_all_games()

    async def update_steam_owned_titles(self, titles: list[str]) -> Any:
        """Persist the full owned-Steam-library titles from the frontend.

        ``appmanifest`` only sees *installed* Steam games, so the
        Ubisoft Steam-linked filter can't hide games the user owns on
        Steam but hasn't installed. The frontend enumerates the full
        owned library (``collectionStore``) and pushes the display names
        here; :mod:`unifideck.stores.ubisoft.library.steam_filter` unions
        them in. Returns ``{"count": <stored>}``.
        """
        from unifideck.steam.owned_games import save_frontend_owned_titles

        safe = [t for t in (titles or []) if isinstance(t, str)]
        return {"count": save_frontend_owned_titles(safe)}

    async def set_active_steam_user(self, account_id: str) -> Any:
        """Persist the live logged-in Steam account id the frontend read.

        The frontend runs *inside* the Steam client and reads the true active
        user from ``window.App.m_CurrentUser`` — the only 100%-correct source.
        The backend otherwise resolves the user from disk heuristics that can
        misfire on multi-account decks (writing shortcuts.vdf to the wrong
        ``userdata/<id>`` → "synced N games, Steam shows 0"). Persisting the id
        (``steam.active_user``, read first by the resolver) fixes the NEXT boot;
        re-binding the live services fixes the CURRENT session immediately.

        Returns ``{"active_user": <id>}`` on success, or ``{"active_user": None}``
        when the id is invalid (non-digit / empty) so the frontend can no-op.
        """
        aid = str(account_id or "").strip()
        if not aid.isdigit() or aid == "0":
            return {"active_user": None}

        from unifideck.steam.current_user import CONFIG_ACTIVE_USER_KEY
        self.config.set(CONFIG_ACTIVE_USER_KEY, aid)

        # Re-bind the live session's per-user paths so the next sync writes to
        # the correct account without waiting for a restart. Also sync the
        # account watcher's view so it doesn't fight the correction.
        coordinator = getattr(self.services, "user_paths_coordinator", None)
        if coordinator is not None:
            coordinator.rebind(aid)
        account_svc = getattr(self.services, "account", None)
        if account_svc is not None and hasattr(account_svc, "_current_user"):
            account_svc._current_user = aid
        return {"active_user": aid}

    async def get_game_info(self, app_id: int) -> Any:
        """Return the full record for a single Unifideck AppID.

        Args:
            app_id: Steam-style AppID (deterministic from
                store + game_id + title).

        Returns:
            Game info dict, or empty / None when unknown.
        """
        # `sync_service.get_game_info` is a synchronous helper
        # (linear scan over `_all_games`) — no coroutine, no
        # await. The previous body had a stray `await` which
        # raised `TypeError: object NoneType can't be used
        # in 'await' expression`.
        #
        # Keep this fast and side-effect-free. The play-section
        # override and the game-info panel both gate on this call
        # resolving (see ``usePlaySection`` / ``GameInfoPanel``); any
        # slow branch here (e.g. a ``legendary info`` / ``gogdl``
        # subprocess) leaves the whole custom UI showing Steam's native
        # section while it waits. Size resolution therefore lives in
        # the separate, non-blocking ``get_game_size_bytes`` RPC, which
        # the frontend fetches on its own (like Last Played).
        return self.sync_service.get_game_info(app_id) if self.sync_service else None

    async def get_game_size_bytes(self, app_id: int) -> int:
        """Resolve the "Space Required" size (bytes) for one AppID.

        Fetched **separately** from :meth:`get_game_info` so the
        sometimes-slow size lookup never delays the play-section
        override (see the note in ``get_game_info``).

        * **Installed** (``installed`` + ``install_path``) → on-disk
          size of the install directory, computed off the event loop.
        * **Not installed** → the store adapter's ``get_game_size``
          download-size lookup. Epic / GOG / Amazon implement it
          (each with its own cache); Ubisoft / Microsoft return
          ``None``. Bounded by ``_SIZE_LOOKUP_TIMEOUT_S`` so a slow or
          offline store can't leave the RPC hanging.

        Always returns an int (``0`` when unknown) — never raises.
        """
        info = self.sync_service.get_game_info(app_id) if self.sync_service else None
        if not isinstance(info, dict):
            return 0

        store = info.get("store")
        game_id = info.get("store_game_id")
        adapter = (
            self.registry.get_store(store) if (self.registry and store) else None
        )

        if info.get("installed"):
            # Exact on-disk size — shared across all stores. See
            # ``stores/shared/installed_size.py``. ``store`` is what lets a
            # wrapper store be sized by its prefix, which for those stores is
            # the game's real footprint and what an uninstall reclaims.
            return await installed_size_bytes(
                adapter, info.get("install_path"), game_id, store,
            )

        if not store or not game_id:
            return 0

        # Persistent cache: a not-installed download size is stable, and
        # the live lookup (legendary/gogdl) takes seconds — so cache it
        # to disk and serve instantly on every later open, even across
        # restarts / reinstalls (the file lives in the data dir).
        cache = get_size_cache(self._size_cache_path())
        cached = await cache.get(store, game_id)
        if cached is not None:
            return cached
        # A store that already failed for this game recently is not worth
        # waiting on again: the caller is a page open, and re-paying a
        # multi-second lookup to display nothing is the worst outcome. The
        # stamp expires (UNKNOWN_TTL_S) so a fixed login or a transient
        # outage recovers on its own.
        if await cache.is_unknown(store, game_id):
            return 0

        if adapter is None or not hasattr(adapter, "get_game_size"):
            return 0
        try:
            size = await asyncio.wait_for(
                adapter.get_game_size(game_id),
                timeout=_SIZE_LOOKUP_TIMEOUT_S,
            )
        except Exception:
            logger.debug(
                "[sync] get_game_size(%s:%s) failed/timed out",
                store, game_id, exc_info=True,
            )
            await cache.mark_unknown(store, game_id)
            return 0
        size_int = int(size or 0)
        if size_int > 0:
            await cache.put(store, game_id, size_int)
        else:
            await cache.mark_unknown(store, game_id)
        return size_int

    async def get_installed_disk_info(self) -> Any:
        """Size + storage location for every installed game, in one call.

        Feeds the Quick-Access "Installed" list's meta line
        (``<size> · Internal|External``). Bulk on purpose: per-row calls
        to :meth:`get_game_size_bytes` would mean one uncached directory
        walk per visible game every time the panel opens — see
        :mod:`unifideck.services.installed_disk_info`, which bounds and
        memoises the walks.

        Returns ``{"<store>:<store_game_id>": {"size_bytes", "location"}}``,
        with games it could not resolve omitted rather than reported as
        zero-sized.
        """
        games = self.sync_service.get_all_games() if self.sync_service else []
        return await collect_installed_disk_info(games, self.registry)

    def _size_cache_path(self) -> str:
        """Path to the persistent download-size cache (in the data dir)."""
        data_dir = "~/.local/share/unifideck"
        cfg = getattr(self, "config", None)
        if cfg is not None:
            with contextlib.suppress(Exception):
                data_dir = (
                    cfg.get("paths.data_dir", None)
                    or cfg.get("data_dir", data_dir)
                    or data_dir
                )
        return str(Path(data_dir).expanduser() / "game_sizes.json")
