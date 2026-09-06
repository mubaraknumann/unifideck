"""Sync execution mixin for :class:`SyncService`.

Extracted from ``core/sync_service.py`` to keep that file under the
550-LOC volumetry cap. Owns the per-run orchestration: setup,
per-store fetch loop, cancellation handling, and the single-store path.

Post-sync finalization (dedup + SYNC_COMPLETE, the phase set, and the
watchdog) lives in ``_SyncFinalizeMixin`` (``sync_finalize_mixin.py``),
also split out for the cap. Construction, queueing (``sync_all`` /
``_enqueue``), the bus event handlers, and ``cancel`` stay in
``SyncService``; read-only queries live in ``_SyncQueriesMixin`` and
result aggregation in ``_SyncResultsMixin``. All consumed state +
sibling-mixin methods are declared as ``TYPE_CHECKING`` annotations —
the host provides them at runtime.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from .sync_availability import refresh_store_availability
from .types import Events, Game, SyncResult

if TYPE_CHECKING:
    from unifideck.core.sync_generation import SyncGeneration
    from unifideck.core.sync_progress import SyncProgress
    from unifideck.event_bus import EventBus
    from unifideck.stores import StoreRegistry
    from unifideck.stores.shared.store_base import StoreBase

logger = logging.getLogger(__name__)

# Per-store ``get_library`` ceiling. The sync runs stores sequentially,
# so a single store that hangs (a wedged Wine/UPC catalog parse, a
# sleeping SD-card mount, a stalled network call) freezes the *whole*
# sync at "store N/N" — the user just sees it stuck and never gets the
# other stores' games. The slowest healthy store observed is well under
# 30s, so 120s is a generous ceiling that still fails a wedged store
# fast enough to finish the sync (the store reports a ``timeout`` error
# and contributes zero games for this run; the next sync retries it).
PER_STORE_FETCH_TIMEOUT_SECONDS = 120

class _SyncRunMixin:
    """Per-run sync orchestration for :class:`SyncService`."""

    # State provided by the host SyncService at runtime.
    _registry: StoreRegistry
    _bus: EventBus
    _cache: Any
    _launcher_path: str
    _cancel_event: asyncio.Event
    _all_games: dict[str, list[Game]]
    _last_sync_time: float | None
    _current_store: str | None
    _progress: SyncProgress
    _post_sync_pending: set[str]
    _registered_phases: set[str]
    _generation: SyncGeneration
    _watchdog_task: asyncio.Task[None] | None
    _current_store_task: asyncio.Task[tuple[list[Game], str | None]] | None
    _cache_snapshot: dict[str, dict[str, Any]] | None

    if TYPE_CHECKING:
        # Sibling-mixin methods composed onto the host SyncService.
        def _save_library_cache(self) -> None: ...
        def _aggregate_results(
            self,
            libraries: dict[str, list[Game]],
            errors: dict[str, str],
            duration_ms: int,
            total: int,
        ) -> SyncResult: ...
        def _flatten(self, libraries: dict[str, list[Game]]) -> list[Game]: ...
        # Post-sync finalize lives in ``_SyncFinalizeMixin``.
        async def _finalize_sync(
            self,
            libraries: dict[str, list[Game]],
            errors: dict[str, str],
            total: int,
            started: float,
            *,
            fetch_artwork: bool = ...,
            resync_artwork: bool = ...,
            is_force: bool = ...,
        ) -> SyncResult: ...

    async def _run_sync(
        self,
        *,
        fetch_artwork: bool = True,
        resync_artwork: bool = False,
        is_force: bool = False,
    ) -> SyncResult:
        """Core sync loop — emits events + handles cancellation.

        Walks every available store sequentially (not parallel, to
        avoid hammering the network and to keep progress linear),
        checking the cancel flag between stores. Each phase is
        delegated to a focused helper so this stays a flat read of
        the orchestration skeleton. The empty-store case is a
        legitimate state, not an error.
        """
        started, available_stores = await self._setup_sync()
        total = len(available_stores)
        if total == 0:
            return await self._sync_no_stores_shortcircuit()
        libraries: dict[str, list[Game]] = {}
        errors: dict[str, str] = {}
        for idx, store in enumerate(available_stores):
            if self._cancel_event.is_set():
                return await self._sync_cancelled_result(idx, total, libraries)
            self._current_store = store.store_name
            await self._emit_progress(store.store_name, idx, total, libraries)
            games, err = await self._fetch_one(store, is_force)
            libraries[store.store_name] = games
            if err is not None:
                errors[store.store_name] = err
            if self._cancel_event.is_set():
                return await self._sync_cancelled_result(
                    idx + 1, total, libraries,
                )
        self._current_store = None
        return await self._finalize_guarded(
            libraries, errors, total, started,
            fetch_artwork=fetch_artwork,
            resync_artwork=resync_artwork,
            is_force=is_force,
        )

    async def _fetch_one(
        self, store: StoreBase, is_force: bool = False,
    ) -> tuple[list[Game], str | None]:
        """Run one store's fetch as a tracked task so ``cancel`` can
        interrupt it mid-await.

        Without this, ``cancel()`` only sets ``_cancel_event`` and the
        loop doesn't notice until the current ``store.get_library()``
        returns — which can take 30+ seconds on slow stores. Task
        cancellation propagates ``CancelledError`` through the store's
        HTTP/subprocess awaits, ending the fetch within milliseconds.
        """
        self._current_store_task = asyncio.create_task(
            self._sync_one_store(store, is_force),
            name=f"sync-store-{store.store_name}",
        )
        try:
            return await asyncio.wait_for(
                self._current_store_task,
                timeout=PER_STORE_FETCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "[SyncService] %s fetch exceeded %ds — skipping it so the "
                "rest of the sync can finish (retried next sync)",
                store.store_name,
                PER_STORE_FETCH_TIMEOUT_SECONDS,
            )
            return [], "timeout"
        except asyncio.CancelledError:
            return [], "cancelled"
        finally:
            self._current_store_task = None

    async def _finalize_guarded(
        self,
        libraries: dict[str, list[Game]],
        errors: dict[str, str],
        total: int,
        started: float,
        *,
        fetch_artwork: bool,
        resync_artwork: bool,
        is_force: bool,
    ) -> SyncResult:
        """``_finalize_sync`` wrapped in the cache-snapshot rollback guard.

        ``_finalize_sync`` does cache-destructive work; if anything
        raises after a partial clear, the snapshot captured in
        ``_setup_sync`` restores the pre-sync state so the next sync
        starts from known-good caches.
        """
        try:
            return await self._finalize_sync(
                libraries, errors, total, started,
                fetch_artwork=fetch_artwork,
                resync_artwork=resync_artwork,
                is_force=is_force,
            )
        except Exception:
            logger.exception(
                "[SyncService] _finalize_sync raised — restoring caches",
            )
            self._restore_cache_snapshot()
            raise

    async def _setup_sync(self) -> tuple[float, list[StoreBase]]:
        """Reset cancel flag, snapshot the registry, emit SYNC_STARTED.

        Returns ``(started, available_stores)`` — monotonic start
        marker (consumed by ``_finalize_sync``) and the store snapshot
        used as the progress denominator.
        """
        self._cancel_event.clear()
        # Stand down any background size warm-up for the duration of the
        # sync. One resumed at plugin boot can still be walking, and it would
        # contend with the metadata/artwork/compat phases for the same store
        # APIs. Resolved sizes are already on disk, and the walk re-spawns
        # once the post-sync phases finish.
        with contextlib.suppress(Exception):
            from unifideck.services import size_backfill
            size_backfill.cancel()
        # Capture every cache's state so a cancel mid-sync can roll
        # back to consistent pre-sync state. Without this, a sync
        # cancelled after the metadata phase wrote a few entries — but
        # before all of them — leaves the cache half populated; the
        # next sync skips the "missing" entries on the cooldown rule.
        if self._cache is not None:
            self._cache_snapshot = self._cache.snapshot()
        else:
            self._cache_snapshot = None
        started = time.monotonic()
        # Open a new generation before anything is emitted, so every
        # event this run produces — SYNC_STARTED here, SYNC_COMPLETE in
        # ``_finalize_sync``, and each POST_SYNC_PHASE_CHANGED echoed by
        # the post-sync modules — carries the same id. The drain sites
        # compare against it to ignore a superseded run's late phase-done
        # (``core/sync_generation.py`` has the measured case).
        run_id = self._generation.begin()
        await refresh_store_availability(self._registry)
        available_stores = self._registry.available()
        store_names = [s.store_name for s in available_stores]
        # Surface stores excluded from this sync. A dropped store never
        # reaches its per-store "fetched N games" log, so without this a
        # silently-skipped store (e.g. GOG after a transient availability
        # probe blip) looks identical to "0 games" in an all-green log
        # (UD-005).
        dropped = [
            s.store_name
            for s in self._registry.all()
            if s.store_name not in store_names
        ]
        if dropped:
            logger.warning(
                "[SyncService] stores excluded from sync "
                "(not available): %s",
                dropped,
            )
        self._progress.start_fetching(len(available_stores))
        self._bus.set_sync_progress(self._progress)
        await self._emit_sync_started(store_names, run_id)
        logger.info(
            "[SyncService] sync starting (%d stores)", len(available_stores),
        )
        return started, available_stores

    async def _emit_sync_started(
        self, store_names: list[str], run_id: int,
    ) -> None:
        """Announce the run on both the ephemeral and durable channels."""
        await self._bus.emit(
            Events.SYNC_STARTED,
            stores=store_names,
            scope="all",
            # Authoritative post-sync phase set for the frontend to drain
            # before prompting the Steam restart — hardcoding it there
            # over-counted and the modal never fired (UD-006).
            registered_phases=sorted(self._registered_phases),
            # Generation this run's phase-done events will carry. The
            # frontend latches it and drops phase events from any other
            # run, so a superseded generation finishing late can no
            # longer drain this run's set and pop the restart modal.
            run_id=run_id,
        )
        # Durable activity event — ephemeral SYNC_STARTED above drives
        # UI; this one feeds the persistent log via ActivityLogService.
        await self._bus.emit(
            Events.LIBRARY_SYNC_STARTED,
            stores=store_names,
            started_at_ms=int(time.time() * 1000),
        )

    def _restore_cache_snapshot(self) -> None:
        """Roll caches back to the pre-sync snapshot if one was taken.

        Idempotent — clears the snapshot after restoring so a second
        call (e.g. cancel happens twice in rapid succession) is a
        no-op. Both branches log; silent failure here would mask the
        cache-state divergence the user is about to see.
        """
        if self._cache_snapshot is None or self._cache is None:
            return
        try:
            self._cache.restore(self._cache_snapshot)
            logger.info("[SyncService] cache snapshot restored after cancel")
        except Exception:
            logger.exception("[SyncService] cache snapshot restore failed")
        self._cache_snapshot = None

    def _populate_app_ids(self, libraries: dict[str, list[Game]]) -> None:
        """Assign every ``Game`` a stable Steam-shortcut AppID.

        Per-store sync methods construct ``Game`` records with
        ``app_id=0`` because the AppID depends on plugin-install state
        they don't know about. We fill it in here, once, so every
        downstream consumer sees a populated id. The AppID is
        ``crc32(launcher_path + title) | 0x80000000`` — anchoring on
        the launcher path (not the per-game exe) keeps it stable across
        install / uninstall.
        """
        if not self._launcher_path:
            logger.warning(
                "[SyncService] launcher_path unset — game.app_id will not "
                "be populated, shortcuts cannot be created",
            )
            return
        from unifideck.services.shortcut.games_map import generate_app_id

        filled = 0
        for games in libraries.values():
            for game in games:
                if game.app_id:
                    continue
                game.app_id = generate_app_id(
                    self._launcher_path,
                    f"{game.store}:{game.store_game_id}",
                )
                filled += 1
        if filled:
            logger.info(
                "[SyncService] populated app_id for %d games", filled,
            )

    async def _sync_no_stores_shortcircuit(self) -> SyncResult:
        """Emit SYNC_COMPLETE with an empty payload and return.

        Used when the registry exposes zero available stores — a
        legitimate state (e.g. all stores offline), not an error. The
        empty SYNC_COMPLETE keeps any UI listener in sync with reality.
        """
        logger.warning("[SyncService] no available stores — nothing to sync")
        await self._bus.emit(
            Events.SYNC_COMPLETE,
            games=[],
            stores_synced=[],
        )
        return SyncResult(
            success=True,
            games=[],
            count=0,
            duration_ms=0,
        )

    async def _sync_cancelled_result(
        self,
        idx: int,
        total: int,
        libraries: dict[str, list[Game]],
    ) -> SyncResult:
        """Emit SYNC_CANCELLED and return the partial result.

        Carries any games already fetched so the caller can keep
        showing the previously-synced state. Rolls every cache back to
        the pre-sync snapshot so partial writes don't persist.
        """
        logger.info(
            "[SyncService] sync cancelled at store %d/%d", idx, total,
        )
        self._restore_cache_snapshot()
        self._progress.mark_cancelled()
        await self._bus.emit(Events.SYNC_CANCELLED)
        await self._bus.emit(
            Events.LIBRARY_SYNC_CANCELLED,
            store_count=total,
            cancelled_at_store=idx,
        )
        return SyncResult(
            success=False,
            error="cancelled",
            games=self._flatten(libraries),
        )

    async def _sync_one_store(
        self, store: StoreBase, is_force: bool = False,
    ) -> tuple[list[Game], str | None]:
        """Fetch one store's library, with broad exception handling.

        Failure path: catch any exception (store implementations may
        raise custom types), log the traceback, emit SYNC_FAILED (for
        machinery) + LAUNCHER_STAGE (a user-facing retry toast carrying
        an ``unifideck://refresh-library/<store>`` deep link), and
        return an empty list with the error string.

        ``get_library`` returning ``None`` is the ABC's "I could not read
        my library" signal and is reported as the ``library_unreadable``
        error rather than as an empty library. Collapsing the two was a
        real defect: the shortcut reconcile treats an empty answer as
        authoritative and deletes every shortcut the store owns, so a
        store whose local state was temporarily unreadable lost the
        user's whole library for that store (audit §3.5, finding B).
        No SYNC_FAILED / toast here — unlike a raise, this is a normal
        state (a vendor client that has never been opened), and a toast
        on every sync would be noise.
        """
        try:
            games = await store.get_library(force=is_force)
            if games is None:
                logger.warning(
                    "[SyncService] %s could not read its library — keeping "
                    "its existing shortcuts rather than treating this as an "
                    "empty library",
                    store.store_name,
                )
                return [], "library_unreadable"
            logger.info(
                "[SyncService] %s: %d games", store.store_name, len(games),
            )
            return games, None
        except Exception as e:
            logger.exception("[SyncService] %s sync failed", store.store_name)
            await self._bus.emit(
                Events.SYNC_FAILED,
                store=store.store_name,
                error=str(e),
            )
            await self._bus.emit(
                Events.LAUNCHER_STAGE,
                severity="warning",
                i18n_key="toasts.library.syncStoreFailed",
                i18n_params={
                    "store": store.store_name,
                    "error": str(e)[:120],
                },
                duration_ms=8000,
                action={
                    "i18n_label_key": "toasts.actions.retryLibrarySync",
                    "target_url": (
                        f"unifideck://refresh-library/{store.store_name}"
                    ),
                },
                store=store.store_name,
            )
            return [], str(e)

    async def _emit_progress(
        self,
        store_name: str,
        idx: int,
        total: int,
        libraries: dict[str, list[Game]],
    ) -> None:
        """Emit ``SYNC_PROGRESS`` — updates the phase tracker + fires event.

        ``total_games`` is the count fetched so far *in this run*
        (``libraries``, built up by the caller's per-store loop) — not
        ``self._all_games``, which still holds the previous run's full
        library until ``_finalize_sync`` overwrites it at the very end.
        Reading ``_all_games`` here made an in-progress sync of a
        handful of stores misreport the prior run's total game count
        as already "synced".
        """
        total_games = sum(len(g) for g in libraries.values())
        self._progress.start_store_sync(store_name, idx, total)
        await self._bus.emit(
            Events.SYNC_PROGRESS,
            store=store_name,
            progress_percent=self._progress.progress_percent,
            total_games=total_games,
            synced_games=total_games,
            current_game=self._progress.current_game,
            status=self._progress.status,
        )
