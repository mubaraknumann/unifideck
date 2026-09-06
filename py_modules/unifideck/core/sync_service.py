"""Multi-store library sync orchestrator.

``SyncService`` walks every registered store, calls its
``get_library`` method, deduplicates the combined output, and
emits bus events at every stage so the frontend can render a
progress bar + per-store status.

This module holds construction, the single-flight queue
(``sync_all`` / ``_enqueue``), the bus event handlers, and
``cancel``. The per-run execution lives in ``_SyncRunMixin``
(``sync_run_mixin.py``), library-cache persistence in
``_SyncCacheMixin`` (``sync_cache_mixin.py``), read-only queries in
``_SyncQueriesMixin``, and result aggregation in
``_SyncResultsMixin`` — split for the 550-LOC volumetry cap; the
public API surface is unchanged.

State retained across sync passes:

* ``_all_games``       — per-store deduplicated library;
* ``_last_sync_time``  — wall-clock timestamp;
* ``_current_store``   — for progress display;
* ``_lock``            — single-flight (only one sync at a time);
* ``_cancel_event``    — cooperative cancel signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.event_bus import EventBus
from unifideck.stores import StoreRegistry

from .sync_cache_mixin import _SyncCacheMixin
from .sync_finalize_mixin import _SyncFinalizeMixin
from .sync_queries_mixin import _SyncQueriesMixin
from .sync_results_mixin import _SyncResultsMixin
from .sync_run_mixin import _SyncRunMixin
from .types import Events, Game, SyncRequest, SyncResult

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager

logger = logging.getLogger(__name__)

# Cooldown defaults — 5 seconds matches staging. Users can override
# via ``sync.cooldown_seconds`` in config.
DEFAULT_COOLDOWN_SECONDS = 5
DEFAULT_COOLDOWN_MS = DEFAULT_COOLDOWN_SECONDS * 1000

class SyncService(
    _SyncCacheMixin, _SyncRunMixin, _SyncFinalizeMixin,
    _SyncQueriesMixin, _SyncResultsMixin,
):
    """Single-flight multi-store library sync orchestrator.

    Composes the run loop (``_SyncRunMixin``), post-sync finalize
    (``_SyncFinalizeMixin``), cache persistence (``_SyncCacheMixin``),
    read-only queries (``_SyncQueriesMixin``), and result aggregation
    (``_SyncResultsMixin``). The split is purely about file size —
    externally this class still exposes the same API surface it
    always did.
    """

    def __init__(
        self,
        registry: StoreRegistry,
        bus: EventBus,
        config: ConfigManager | None = None,
        launcher_path: str = "",
        cache: CacheManager | None = None,
    ) -> None:
        """Initialise with the store registry + event bus + optional config.

        Lock + cancel event are constructed eagerly so ``sync_all``
        can be called immediately after construction without an init
        pass.

        Args:
            registry: ``StoreRegistry`` to enumerate available stores.
            bus: event bus for status / progress events.
            config: optional ``ConfigManager`` (tracked-stores list).
            launcher_path: absolute path to ``bin/unifideck-launcher``;
                combined with the game title to produce a stable
                Steam-shortcut AppID via ``generate_app_id``. Invariant
                across install / uninstall, which ShortcutService and
                ArtworkService both rely on.
            cache: optional ``CacheManager``. When provided, a snapshot
                is taken at sync start and restored on cancel —
                protects caches from partial writes on mid-sync abort.
        """
        self._registry = registry
        self._bus = bus
        self._config = config
        self._cache = cache
        # Read once at construction — the value is small enough that a
        # re-read at every sync is overkill; restart to pick up changes.
        self._cooldown_ms = self._resolve_cooldown_ms()
        self._launcher_path = launcher_path
        self._lock = asyncio.Lock()
        # ``time.monotonic()`` timestamp of the last ``self._lock``
        # acquisition, ``None`` while free. Lets ``_enqueue`` report how
        # long an in-flight sync has been running when a new request
        # queues behind it — the single fact that distinguishes a merely
        # slow sync from a permanently wedged lock (UD-013).
        self._lock_acquired_at: float | None = None
        # Smaller lock guarding ``_pending_request`` reads/writes,
        # distinct from ``_lock`` (which gates whole runs) so an enqueue
        # from another task doesn't wait for the in-flight sync.
        self._request_lock = asyncio.Lock()
        self._pending_request: SyncRequest | None = None
        self._cancel_event = asyncio.Event()
        self._all_games: dict[str, list[Game]] = {}
        self._last_sync_time: float | None = None
        self._load_library_cache()
        self._current_store: str | None = None
        self._init_progress_tracking()
        self._subscribe_events()

    def _init_progress_tracking(self) -> None:
        """Set up the per-run progress tracker + post-sync phase state."""
        # Per-sync-run progress tracker, consumed by the frontend's
        # 500ms polling loop via ``get_sync_progress → to_dict()``.
        from unifideck.core.sync_progress import SyncProgress
        self._progress = SyncProgress()
        self._post_sync_pending: set[str] = set()
        # Run ids + last-completed-chain bookkeeping. Kept in its own
        # module because both facts need real explanation; see
        # ``core/sync_generation.py`` for the measured race they fix.
        from unifideck.core.sync_generation import SyncGeneration
        self._generation = SyncGeneration()
        # Phases that post-sync services register; seeded into
        # ``_post_sync_pending`` at every finalize so mark_complete only
        # fires once every registered service reports done. Always-on
        # phases are pre-listed; others register at bootstrap.
        self._registered_phases: set[str] = {"artwork", "metadata"}
        self._watchdog_task: asyncio.Task[None] | None = None
        # In-flight per-store fetch task, held so :meth:`cancel` can
        # interrupt a slow ``store.get_library()`` mid-await.
        self._current_store_task: (
            asyncio.Task[tuple[list[Game], str | None]] | None
        ) = None
        # Snapshot of every CacheManager store, captured at
        # ``_setup_sync`` time and consumed on cancel.
        self._cache_snapshot: dict[str, dict[str, Any]] | None = None

    def _subscribe_events(self) -> None:
        """Wire the bus handlers that keep ``_all_games`` live."""
        self._bus.on(
            Events.POST_SYNC_PHASE_CHANGED, self._on_post_sync_phase,
        )
        # Keep ``_all_games`` in sync with shortcut install-state flips
        # so the GOG tab + detail-page UI work immediately after
        # install/uninstall (without waiting for the next full sync).
        # ``mark_installed``/``mark_uninstalled`` preserve the appid, so
        # we only touch ``installed`` / ``exe_path`` / ``install_path``.
        self._bus.on(
            Events.SHORTCUT_INSTALL_STATE_CHANGED,
            self._on_shortcut_install_state_changed,
        )

    async def sync_all(
        self,
        *,
        force: bool = False,
        fetch_artwork: bool = True,
        resync_artwork: bool = False,
        source: str = "manual",
    ) -> SyncResult:
        """Run a full multi-store sync. Queues behind an in-flight sync.

        Wraps the args in a :class:`SyncRequest` and dispatches through
        :meth:`_enqueue`. A second concurrent call merges into
        ``_pending_request`` and runs as soon as the current sync
        releases the lock; the response carries ``restart_pending=True``.

        ``force=True`` is reserved for tests and admin actions that need
        to bypass the queue entirely. Production callers leave it False.

        Args:
            force: bypass the lock + queue. Use sparingly.
            fetch_artwork: when ``False``, skip the artwork phase.
            resync_artwork: when ``True``, ArtworkService clears its
                SGDB cache + ignores ``has_artwork`` so every game gets
                a fresh download.
            source: provenance string — ``"manual"`` (default),
                ``"auth:<store>"``, ``"background"``, ``"scheduled"``.

        Returns:
            ``SyncResult`` from the full sync, or a queued-response when
            the request was deferred.
        """
        request = SyncRequest(
            kind="force" if force else "sync",
            source=source,
            fetch_artwork=fetch_artwork,
            resync_artwork=resync_artwork,
        )
        is_force = request.kind == "force"
        if force:
            # Force path is a hard bypass — no queue interaction so
            # tests / admin actions can drive the loop without
            # interference. Skip _enqueue; go straight to the lock.
            async with self._lock:
                self._lock_acquired_at = time.monotonic()
                try:
                    return await self._run_sync(
                        fetch_artwork=fetch_artwork,
                        resync_artwork=resync_artwork,
                        is_force=is_force,
                    )
                finally:
                    self._lock_acquired_at = None
        return await self._enqueue(request)

    async def _enqueue(self, request: SyncRequest) -> SyncResult:
        """Queue or run a :class:`SyncRequest`. Merges if a sync is in flight.

        Two paths:

        * **Lock free** — acquire it, run ``_run_sync``, then drain any
          request enqueued during the run (recursing to run it too).
        * **Lock held** — merge into ``_pending_request`` (force wins,
          flags OR together) and return a "queued" :class:`SyncResult`
          with ``restart_pending=True``.

        The merge step is what makes auth-chained syncs work — login
        finishes mid-sync, the post-auth request folds into the queue
        and runs automatically once the current sync completes.
        """
        if self._lock.locked():
            async with self._request_lock:
                merged = (
                    self._pending_request.merge(request)
                    if self._pending_request is not None
                    else request
                )
                self._pending_request = merged
            held_for = (
                f"{time.monotonic() - self._lock_acquired_at:.1f}s"
                if self._lock_acquired_at is not None
                else "unknown"
            )
            logger.info(
                "[SyncService] sync request queued behind in-flight "
                "(source=%s, kind=%s, held=%s)",
                request.source, request.kind, held_for,
            )
            return SyncResult(
                success=True,
                games=[],
                count=0,
                duration_ms=0,
                restart_pending=True,
                source=request.source,
            )
        async with self._lock:
            self._lock_acquired_at = time.monotonic()
            try:
                current = request
                while True:
                    result = await self._run_sync(
                        fetch_artwork=current.fetch_artwork,
                        resync_artwork=current.resync_artwork,
                        is_force=current.kind == "force",
                    )
                    result.source = current.source
                    # Drain anything queued during the run.
                    async with self._request_lock:
                        next_req = self._pending_request
                        self._pending_request = None
                    if next_req is None:
                        return result
                    logger.info(
                        "[SyncService] draining queued sync (source=%s, kind=%s)",
                        next_req.source, next_req.kind,
                    )
                    current = next_req
            finally:
                self._lock_acquired_at = None

    def _resolve_cooldown_ms(self) -> int:
        """Read ``sync.cooldown_seconds`` from config, default 5s.

        Called once at init — small enough that re-reading at every
        sync is overhead without benefit. Users who change the config
        must restart the plugin to pick up the new value.
        """
        if self._config is None:
            return DEFAULT_COOLDOWN_MS
        try:
            seconds = self._config.get(
                "sync.cooldown_seconds", DEFAULT_COOLDOWN_SECONDS,
            )
            ms = int(float(seconds) * 1000)
        except (TypeError, ValueError):
            return DEFAULT_COOLDOWN_MS
        return max(ms, 0)

    def register_post_sync_phase(self, phase: str) -> None:
        """Declare that a post-sync service will emit ``phase``-done events.

        Called by services at bootstrap (e.g. ``CompatibilityService``
        registering ``"proton_meta"``). The registered phase is added
        to ``_post_sync_pending`` at the start of every sync, so
        ``mark_complete`` only fires once every registered service has
        reported done. Without this, a service whose phase hadn't been
        pre-declared would be ignored and the bar would race to
        "complete" before it finished.
        """
        self._registered_phases.add(phase)

    async def request_auth_sync(self, store: str) -> SyncResult:
        """Queue a post-login sync. Called by AuthDispatcher after store auth.

        Without this, a successful store login while a sync is running
        would silently drop the refresh. Routing through ``_enqueue``
        means the new library shows up the moment the current sync
        finishes.
        """
        return await self.sync_all(source=f"auth:{store}")

    async def sync_single_store(
        self, store_name: str,
    ) -> tuple[bool, str | None]:
        """Sync just one store and merge its result into the running library.

        Used by the ``refresh-library`` URI verb. Unlike ``sync_all``,
        doesn't hold the single-flight lock — the caller is responsible
        for not racing a full sync. After fetching, runs the full dedup
        pass over the merged state so cross-store consistency holds.

        Returns:
            ``(success_bool, optional_error_string)``.
        """
        store = self._registry.get_store(store_name)
        if store is None:
            logger.warning(
                "[SyncService] refresh-library: unknown store %r", store_name,
            )
            return False, "unknown_store"
        await self._bus.emit(
            Events.SYNC_STARTED,
            stores=[store_name],
            scope="single",
        )
        await self._emit_progress(store_name, 0, 1, {})
        games, err = await self._sync_one_store(store)
        if self._all_games is None:
            self._all_games = {}  # type: ignore[unreachable]  # registry-miss fallback
        self._all_games[store_name] = games
        self._last_sync_time = time.time()
        self._save_library_cache()
        await self._bus.emit(
            Events.SYNC_COMPLETE,
            games=self._flatten(self._all_games),
            stores_synced=[store_name],
            errors={store_name: err} if err else {},
            duration_ms=0,
        )
        return err is None, err

    def _on_post_sync_phase(self, **kwargs: Any) -> None:
        """Handle POST_SYNC_PHASE_CHANGED (completion only).

        Phase starts are triggered by SyncService directly. Completions
        are tracked in ``_post_sync_pending`` — only when every
        registered phase reports done do we ``mark_complete()``. This
        prevents a race where metadata finishes before artwork (both
        tasks are spawned concurrently at SYNC_COMPLETE time).
        """
        phase = kwargs.get("phase")
        active = bool(kwargs.get("active", True))
        if active:
            return
        # Drop a superseded run's phase-done before it can touch this run's
        # pending set: an orphaned artwork batch finishing minutes late used
        # to mark the *current* sync complete mid-download.
        if self._generation.is_stale(kwargs):
            logger.debug(
                "[SyncService] ignoring stale %s phase-done (run %s != %s)",
                phase, kwargs.get("run_id"), self._generation.run_id,
            )
            return
        total = kwargs.get("total", 0)
        if phase == "artwork":
            self._progress.artwork_synced = total
        # The metadata phase tracks its own per-source counters
        # (steam/unifidb/metacritic) directly; this handler only needs
        # to clear the phase from the pending set below.
        pending: set[str] = getattr(self, "_post_sync_pending", set())
        pending.discard(phase)
        if not pending:
            # Preserve a cancelled status — services' try/finally still
            # emits the phase-done event after cancel, which would
            # otherwise flip status from "cancelled" to "complete".
            if self._progress.status != "cancelled":
                self._progress.mark_complete()
                self._spawn_size_backfill()
                self._record_chain_complete()
            self._bus.set_sync_progress(None)

    def resume_size_backfill(self) -> None:
        """Restart an interrupted size warm-up at plugin boot.

        The walk is an asyncio task inside the plugin's own process, and that
        process is restarted independently of Steam and of ``plugin_loader``
        — including in the most likely moment of all: right after a sync,
        when the user restarts Steam so new shortcuts and artwork load.

        Nothing is lost when that happens (every resolved size is flushed to
        ``game_sizes.json`` as it lands, so at most the two in-flight lookups
        are discarded), but without this the *remaining* games would sit
        un-warmed until the next sync. Re-running here makes the warm-up
        eventually-complete across any number of restarts.

        Safe to call unconditionally: the walk skips games that already have
        a cached size, so once the library is warm this costs one pass over
        the in-memory list and zero network calls.
        """
        if not self._all_games:
            return
        self._spawn_size_backfill()

    def _spawn_size_backfill(self) -> None:
        """Warm the download-size cache once every post-sync phase is done.

        "Space Required" is looked up lazily per game and each lookup is a
        live store call, so an un-warmed cache means a multi-second gap on
        the App-Details page — every game, the first time it is opened.

        Deliberately started HERE rather than at SYNC_COMPLETE: the metadata
        / artwork / compat phases are all network-bound and already serialise
        against each other, so starting a fourth walk alongside them would
        just contend for the same bandwidth and store rate limits. By this
        point the progress bar has hit 100% and the pipeline is idle.

        Fire-and-forget and fully guarded — this is an optimisation, and it
        must never be able to fail a sync that has already succeeded.
        """
        try:
            from unifideck.services import size_backfill
            games = [g for games in self._all_games.values() for g in games]
            size_backfill.spawn(
                self._registry, games, self._size_cache_path(),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("[SyncService] size backfill not started: %s", e)

    def _size_cache_path(self) -> str:
        """Path to the persistent download-size cache (in the data dir).

        Mirrors ``SyncRPCMixin._size_cache_path`` — both must resolve to the
        same file or the warm-up would fill a cache nothing reads.
        """
        data_dir = "~/.local/share/unifideck"
        cfg = self._config
        if cfg is not None:
            with contextlib.suppress(Exception):
                data_dir = (
                    cfg.get("paths.data_dir", None)
                    or cfg.get("data_dir", data_dir)
                    or data_dir
                )
        return str(Path(data_dir).expanduser() / "game_sizes.json")

    async def _on_shortcut_install_state_changed(self, **kwargs: Any) -> None:
        """Flip the in-memory Game record's installed state.

        Emitted by ShortcutService.mark_installed / mark_uninstalled
        whenever a shortcut's install state flips. We mirror the change
        into ``_all_games`` (preserving ``app_id`` — the launcher-
        anchored id stays valid) and persist so a Decky reload doesn't
        drop it. Lookup is strict ``(store, store_game_id)``.
        """
        store = kwargs.get("store")
        store_game_id = kwargs.get("store_game_id")
        installed = kwargs.get("installed")
        if (
            not isinstance(store, str)
            or not isinstance(store_game_id, str)
            or not isinstance(installed, bool)
        ):
            return
        exe_path = kwargs.get("exe_path", "") or ""
        install_path = kwargs.get("install_path", "") or ""

        async with self._lock:
            target: Game | None = None
            for game in self._all_games.get(store, []):
                if game.store_game_id == store_game_id:
                    target = game
                    break
            if target is None:
                logger.warning(
                    "[SyncService] %s:%s state-change ignored — no "
                    "matching record in _all_games",
                    store, store_game_id,
                )
                return
            target.installed = installed
            target.exe_path = exe_path if installed else ""
            target.install_path = install_path if installed else ""
            self._save_library_cache()
        logger.info(
            "[SyncService] flipped installed=%s for %s:%s (app_id=%s)",
            installed, store, store_game_id, target.app_id,
        )

    async def cancel(self) -> bool:
        """Request cancellation of the in-flight sync.

        Cooperative: the sync loop checks ``self._cancel_event`` between
        stores; ArtworkService and MetadataService check
        ``progress.status == "cancelled"`` between per-game iterations.
        The bus emit signals services that don't poll the progress
        object so they can flush queued work.

        Returns ``False`` immediately if no sync is running; otherwise
        ``True`` — the running code finds out via ``_cancel_event``
        and/or ``progress.status`` and exits at its next checkpoint.
        """
        if not self._lock.locked():
            return False
        self._cancel_event.set()
        # Mark progress cancelled so the post-sync service loops see it
        # at their next iteration (essential for cancellation mid-post-
        # sync, where the per-store loop has already returned).
        self._progress.mark_cancelled()
        # Forcefully interrupt the in-flight store fetch so the loop
        # doesn't wait for the current ``store.get_library()`` to
        # finish; CancelledError propagates through its awaits and
        # ``_run_sync``'s ``except`` turns it into a clean result.
        current_task = self._current_store_task
        if current_task is not None and not current_task.done():
            current_task.cancel()
        # Broadcast so non-polling listeners (frontend SyncContext) get
        # notified. Idempotent — a second emit has no observable effect.
        await self._bus.emit(Events.SYNC_CANCELLED)
        logger.info("[SyncService] cancel requested")
        return True
