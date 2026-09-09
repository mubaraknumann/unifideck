"""services/download/worker.py — Worker loop + install dispatch.

Queue consumer: polls pending queue, enforces concurrency cap,
dispatches each install to the right store via the registry,
emits ``DOWNLOAD_{STARTED,COMPLETE,FAILED}``. Mixin — only
touches host state, no I/O primitives of its own.

Refactor history (2026-05-14): ``_worker_loop`` was a single
async function at CC=16. The locked critical section, the
post-lock dispatch, and the error/cancel envelope were all
inlined, making the main loop hard to scan. Split into two
private helpers so the outer loop reads as
``while: pop → dispatch → sleep``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from unifideck.core import stale_installs
from unifideck.launcher.wrapper_stores import (
    is_wrapper_store,
    uses_manual_download_phase,
)

from .installed_game import build_installed_game
from .models import MAX_FINISHED_HISTORY, DownloadItem, classify_download_error
from .worker_helpers import (
    apply_dict_progress,
    prefix_warmup_supported,
    track_task,
)
from .wrapper_signals import dispatch_wrapper_install

if TYPE_CHECKING:
    from unifideck.core.types import InstallResult
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry
    from unifideck.stores.shared.store_base import StoreBase

logger = logging.getLogger(__name__)

# Polling cadence — kept as module constants so a future test
# can monkeypatch them to speed up integration runs without
# touching the loop logic itself.
_POLL_INTERVAL_SEC: float = 1.0
_ERROR_BACKOFF_SEC: float = 5.0
# Terminal phase, set alongside a terminal ``status``. The phase used to be
# left at whatever it last was, so every finished row in
# ``download_history.json`` still read ``"preparing"`` — which is what a stale
# frontend snapshot then rendered as "SETTING UP GAME..." forever.
_PHASE_DONE = "complete"


class _WorkerMixin:
    """Queue worker + install dispatcher for DownloadService.

    Attribute declarations satisfy mypy; at runtime they come
    from the host class.
    """

    _bus: EventBus
    _registry: StoreRegistry
    _lock: asyncio.Lock
    _max_concurrent: int
    _queue: list[DownloadItem]
    _running: dict[str, DownloadItem]
    _launcher_path: str
    # Note: ``_prefix_warmup`` and ``_on_complete_callback`` are intentionally
    # NOT declared here — they're optional host-set hooks accessed via
    # ``getattr(self, ..., None)`` so the mixin stays standalone-safe.

    async def _worker_loop(self) -> None:
        """Poll the queue and dispatch installs until cancelled.

        Each iteration: pop items eligible to start under the
        lock, dispatch them outside the lock (so the queue save
        and ``create_task`` don't block other producers), then
        sleep. Cancellation and unexpected errors are handled
        as flat branches at the top level.
        """
        while True:
            try:
                to_start = await self._pop_ready_items()
                if to_start:
                    await self._dispatch_items(to_start)
                await asyncio.sleep(_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception:
                # Any unexpected error: log with full traceback,
                # back off harder than the regular poll so we
                # don't burn CPU if the cause is persistent.
                logger.exception(
                    "[DownloadWorker] unhandled error in loop",
                )
                await asyncio.sleep(_ERROR_BACKOFF_SEC)

    async def _pop_ready_items(self) -> list[DownloadItem]:
        """Pop queue items eligible to start under the worker lock.

        Holds ``self._lock`` while mutating both ``self._queue``
        and ``self._running`` so concurrent producers (queue
        adders) see a consistent state. Returns the popped items
        for the caller to dispatch *outside* the lock — keeps
        the critical section as short as possible.
        """
        to_start: list[DownloadItem] = []
        async with self._lock:
            while len(self._running) < self._max_concurrent and self._queue:
                item = self._queue.pop(0)
                key = f"{item.store}:{item.game_id}"
                self._running[key] = item
                to_start.append(item)
        return to_start

    async def _dispatch_items(
        self,
        to_start: list[DownloadItem],
    ) -> None:
        """Persist the queue change, then spawn install tasks.

        Persistence runs first so a crash between pop and
        spawn doesn't resurrect items we've already committed
        to running. ``_save_queue`` is looked up dynamically:
        the queue-persistence mixin is optional, so the host
        class may or may not provide it.
        """
        save_method = getattr(self, "_save_queue", None)
        if callable(save_method):
            await save_method()
        running_tasks = getattr(self, "_running_tasks", None)
        for item in to_start:
            task = asyncio.create_task(self._run_install(item))
            track_task(task)
            # Register the task so DownloadService.cancel can kill
            # a running install. The mixin host sets this up;
            # ``getattr`` keeps the worker mixin standalone-safe.
            if running_tasks is not None:
                running_tasks[f"{item.store}:{item.game_id}"] = task

    async def _run_install(self, item: DownloadItem) -> None:
        """Execute one install via ``StoreBase.install_game``.

        Flow: resolve store via registry (missing → emit
        DOWNLOAD_FAILED + cleanup), emit DOWNLOAD_STARTED,
        dispatch to the correct store with per-store argument
        conventions, classify the result (``InstallResult``) or
        any exception via ``classify_download_error``, emit
        DOWNLOAD_COMPLETE or DOWNLOAD_FAILED with the classified
        error, always ``_cleanup_running(item)`` in a finally
        block.
        """
        key = f"{item.store}:{item.game_id}"
        try:
            await self._execute_install(item, key)
        except asyncio.CancelledError:
            # ``DownloadService.cancel`` killed the running task. Mark
            # + emit, then re-raise so the task machinery sees a clean
            # cancellation.
            await self._mark_cancelled(item, key)
            raise
        except Exception as e:
            logger.exception(
                "[DownloadWorker] exception during install of %s",
                key,
            )
            await self._emit_failure(item, str(e), key)
        finally:
            self._cleanup_running(item)

    async def _execute_install(self, item: DownloadItem, key: str) -> None:
        """Resolve the store, dispatch the install, route the result."""
        store = self._registry.get_store(item.store)
        if not store:
            raise RuntimeError(f"Store {item.store} not found in registry")
        # NOTE: there is deliberately no store-name branch here for the
        # cloud-only store. One existed — `_reject_microsoft` — and it
        # bypassed `_emit_failure`, so the queue row never reached "failed"
        # and never got an `error` set, while the toast echoed a hardcoded
        # English sentence straight past `friendlyDownloadError`'s code
        # table. The store itself now refuses with `not_supported` (audit
        # §3.5, register item 11), so the ordinary failure path below does
        # the whole job: correct row state, one classified error, one
        # translated toast. A store's own refusal is the right place for
        # this; the worker should not need to know which stores can install.
        await self._begin_install(item)

        async def progress_cb(progress: float | dict[str, Any]) -> None:
            await self._update_progress(item, progress)

        result = await self._dispatch_install(item, store, progress_cb, key)
        if result.success:
            await self._on_install_success(item, result, store, key)
        else:
            logger.error(
                "[DownloadWorker] failed install for %s: %s",
                key,
                result.error,
            )
            await self._emit_failure(item, result.error, key)

    async def _begin_install(self, item: DownloadItem) -> None:
        """Flip the item to ``running`` and emit DOWNLOAD_STARTED.

        Moving to "running" progresses the UI label and lets
        status-keyed consumers (cancel paths, progress visibility)
        see the right state.
        """
        from unifideck.core.types.events import Events

        item.status = "running"
        item.start_time = time.time()
        # Wrapper stores are vendor-client-driven installs — there is no
        # real download to measure. Start in the indeterminate "manual"
        # phase so the UI never shows a "DOWNLOADING… 0.0%" frame before the
        # first progress emit lands. The store's progress callback keeps it
        # on "manual".
        if uses_manual_download_phase(item.store):
            item.download_phase = "manual"
        if self._bus:
            await self._bus.emit(Events.DOWNLOAD_STARTED, item=item.to_dict())

    async def _dispatch_install(
        self,
        item: DownloadItem,
        store: StoreBase,
        progress_cb: Any,
        key: str,
    ) -> InstallResult:
        """Call the correct store entry point for *item*.

        Updates use the store's genuine ``update_game`` command; otherwise
        per-store install signatures differ — the wrapper stores are
        keyword-only (see
        :func:`~.wrapper_signals.dispatch_wrapper_install`), while
        Epic/Amazon/GOG take ``base_path`` positionally.
        """
        if item.is_update:
            logger.info("[DownloadWorker] starting update for %s", key)
            # A wrapper store's update is the same vendor-client operation as
            # its install, so it needs the same ``on_ready`` signal — without
            # it the client is never opened and the update waits for a window
            # nobody asked for.
            if is_wrapper_store(item.store):
                return await dispatch_wrapper_install(
                    self._bus,
                    item,
                    store,
                    progress_cb,
                )
            return await store.update_game(item.game_id, progress_cb=progress_cb)
        logger.info("[DownloadWorker] starting install for %s", key)
        # Clear stale local state first. A store CLI's install records can
        # outlive the files they name (manual delete, moved SD card, failed
        # "Delete all data"), and then the CLI treats an install request as a
        # no-op: nile exited 0 in 1.4s having downloaded nothing, and the
        # install could never succeed however often the user retried. nile
        # keeps TWO such records and the cached manifest — not installed.json
        # — is the one that vetoes the download; see stale_installs. Only
        # reachable for a fresh install — an update wants its files intact.
        await asyncio.to_thread(
            stale_installs.reconcile_for_install,
            item.store,
            item.game_id,
        )
        if is_wrapper_store(item.store):
            return await dispatch_wrapper_install(
                self._bus,
                item,
                store,
                progress_cb,
            )
        # GOG and Epic honour a user-picked install language (GOG via
        # gogdl's --lang, Epic via a legendary SDL install tag); the
        # other stores don't accept the kwarg, so only pass it to those.
        extra: dict[str, Any] = {}
        if item.store in ("gog", "epic") and item.language:
            extra["language"] = item.language
            logger.info(
                "[DownloadWorker] %s install language=%s",
                key,
                item.language,
            )
        # A separate staging directory for the downloaded artifact, for a
        # store whose install is "fetch an archive, then unpack it" and whose
        # archive therefore needs room of its own. Keyed off the field, not
        # off a store name: only the store that accepts the kwarg ever sets
        # it (nothing else populates ``DownloadItem.download_dir``), so the
        # worker does not need to know which store that is — same reasoning
        # as the deleted ``_reject_microsoft`` branch above.
        if item.download_dir:
            extra["download_dir"] = item.download_dir
            logger.info(
                "[DownloadWorker] %s download_dir=%s", key, item.download_dir,
            )
        return await store.install_game(  # type: ignore[call-arg]
            item.game_id,
            item.install_path or None,
            progress_cb=progress_cb,
            **extra,
        )

    async def _on_install_success(
        self,
        item: DownloadItem,
        result: InstallResult,
        store: StoreBase,
        key: str,
    ) -> None:
        """Finalise the install, then run the cancellable prefix warmup.

        The order here is the fix for two bugs and matters more than it looks.

        The warmup used to run FIRST, ahead of every line below. That made
        cancelling it destructive: ``CancelledError`` is a ``BaseException``, so
        it slipped past the warmup's ``except Exception`` into
        ``_run_install``'s cancel handler and marked the item cancelled — no
        ``DOWNLOAD_COMPLETE``, no shortcut flip, no ``games.map`` entry, for a
        game already fully on disk. Finalising first makes the install durable
        before anything cancellable starts, so an abandoned warmup now costs
        only a slower first launch.

        The row must still be visible (and cancellable) *during* the warmup,
        which is why the item is only marked terminal after it: ``get_queue``
        hides terminal rows. A stale frontend snapshot of a phase the backend
        had already finished is exactly the "SETTING UP GAME... / Cancel
        Failed: not_found" report this came from.
        """
        item.progress = 100.0
        result_install_path = getattr(result, "install_path", None)
        if result_install_path:
            item.install_path = result_install_path
        if not self._warmup_applies(item):
            self._mark_item_complete(item)
            await self._finalise_install(item, result, store, key)
            return
        await self._finalise_install(item, result, store, key)
        await self._run_prefix_warmup(item, key)
        self._mark_item_complete(item)
        # Nothing else fires after this point — ``_cleanup_running`` is silent —
        # so the terminal state above needs an event of its own or the frontend
        # renders the warmup row forever.
        await self._emit_queue_refresh(item)

    async def _finalise_install(
        self,
        item: DownloadItem,
        result: InstallResult,
        store: StoreBase,
        key: str,
    ) -> None:
        """Publish the install: DOWNLOAD_COMPLETE plus the post-install hook.

        Everything that makes an install durable lives here. The ``Game``
        record is built so the ShortcutService listener flips the shortcut's
        install tag; DOWNLOAD_COMPLETE is the only event this needs, since
        ``mark_installed`` then emits SHORTCUT_INSTALL_STATE_CHANGED, which is
        what every install-state reader subscribes to. No separate install
        event — the shortcut and its cover art were created during the library
        sync and ``mark_installed`` preserves the appid, so re-fetching artwork
        here would only duplicate SteamGridDB lookups. The host's
        ``on_complete`` callback then writes the ``.unifideck-id`` marker,
        updates ``games.map``, and invalidates the caches.
        """
        from unifideck.core.types.events import Events

        logger.info("[DownloadWorker] completed install for %s", key)
        game = await build_installed_game(
            item, result, store, getattr(self, "_launcher_path", ""),
        )
        if self._bus:
            await self._bus.emit(
                Events.DOWNLOAD_COMPLETE,
                item=item.to_dict(),
                game=game,
            )
        on_complete = getattr(self, "_on_complete_callback", None)
        if callable(on_complete):
            try:
                await on_complete(item)
            except Exception:
                logger.exception("[DownloadWorker] on_complete callback failed")

    def _mark_item_complete(self, item: DownloadItem) -> None:
        """Put ``item`` into its terminal success state.

        Resetting the phase matters: it used to be left wherever the install
        last set it, so every finished row in ``download_history.json`` still
        read ``"preparing"``. Failure and cancel paths deliberately keep their
        phase, where it says *which step* died and is worth having.
        """
        item.status = "complete"
        item.download_phase = _PHASE_DONE
        item.end_time = time.time()

    async def _emit_queue_refresh(self, item: DownloadItem) -> None:
        """Nudge the frontend into re-reading the queue.

        The frontend refetches on DOWNLOAD_STARTED without inspecting the
        payload, which makes it the cheap way to publish a row change that has
        no event of its own. Pre-existing idiom, not a new one: the wrapper
        stores' "manual" phase is surfaced the same way.
        """
        if not self._bus:
            return
        from unifideck.core.types.events import Events

        await self._bus.emit(Events.DOWNLOAD_STARTED, item=item.to_dict())

    def _warmup_applies(self, item: DownloadItem) -> bool:
        """Whether this install gets an install-time prefix warmup.

        Store and depot eligibility lives in ``worker_helpers``. The rest is
        whether the host actually wired both hooks — the launcher-subset
        bootstrap wires neither.
        """
        if not prefix_warmup_supported(item):
            return False
        has_hook = callable(getattr(self, "_prefix_warmup", None))
        return has_hook and getattr(self, "_warmup_runner", None) is not None

    async def _run_prefix_warmup(self, item: DownloadItem, key: str) -> None:
        """Run install-time prefix setup, surfaced as a "preparing" phase.

        Callers must have checked :meth:`_warmup_applies`. Cancellation
        semantics belong to :class:`~.warmup_runner.PrefixWarmupRunner`: a
        cancel aimed at the warmup is absorbed there so the install still
        finalises, while an outer teardown propagates. Best-effort either way —
        a timeout or a failure is logged and the install completes, with the
        launch-time path as the fallback.
        """
        hook: Any = getattr(self, "_prefix_warmup", None)
        runner: Any = getattr(self, "_warmup_runner", None)
        item.download_phase = "preparing"
        await self._emit_queue_refresh(item)
        logger.info("[DownloadWorker] running prefix warmup for %s", key)
        outcome = await runner.run(key, lambda: hook(item))
        logger.info("[DownloadWorker] prefix warmup %s for %s", outcome, key)

    async def _mark_cancelled(self, item: DownloadItem, key: str) -> None:
        """Mark the item cancelled and emit DOWNLOAD_CANCELLED."""
        from unifideck.core.types.events import Events

        item.status = "cancelled"
        item.end_time = time.time()
        logger.info("[DownloadWorker] cancelled install for %s", key)
        if self._bus:
            await self._bus.emit(Events.DOWNLOAD_CANCELLED, item=item.to_dict())

    async def _emit_failure(self, item: DownloadItem, error: Any, key: str) -> None:
        """Mark the item failed, classify the error, emit DOWNLOAD_FAILED."""
        from unifideck.core.types.events import Events

        item.status = "failed"
        item.error = str(error or "")
        item.end_time = time.time()
        error_type = classify_download_error(error or "")  # type: ignore[arg-type]
        if self._bus:
            await self._bus.emit(
                Events.DOWNLOAD_FAILED,
                item=item.to_dict(),
                error=error,
                error_type=error_type,
            )

    def _cleanup_running(self, item: DownloadItem) -> None:
        """Remove a finished item from ``self._running``.

        No-op when the key is already gone (idempotent so
        failure paths can call it without tracking state).
        Also appends to ``self._finished`` (capped) so the
        Downloads page shows a history entry after a successful
        completion (or failure / cancel).

        History is keyed on the item id, one row per game. Retrying an
        install used to leave a row per attempt, so a game the user had
        tried twice rendered as two identical cards under "Failed" —
        indistinguishable from two different problems. The newest
        outcome is the only one that describes the game's current state,
        so it replaces the older one rather than stacking on top of it.
        """
        key = f"{item.store}:{item.game_id}"
        self._running.pop(key, None)
        running_tasks = getattr(self, "_running_tasks", None)
        if running_tasks is not None:
            running_tasks.pop(key, None)
        finished = getattr(self, "_finished", None)
        if isinstance(finished, list):
            # ``key`` is exactly the ``id`` that ``to_dict`` synthesises and
            # the frontend keys its rows on.
            for i in range(len(finished) - 1, -1, -1):
                prev = finished[i]
                if f"{prev.store}:{prev.game_id}" == key:
                    del finished[i]
            finished.append(item)
            # Cap the in-memory history (FIFO). Matches what we persist
            # + show in the QAM "Recently finished" list.
            if len(finished) > MAX_FINISHED_HISTORY:
                del finished[: len(finished) - MAX_FINISHED_HISTORY]
            # Persist the updated history (best-effort, fire-and-forget)
            # so it survives restarts / plugin reinstalls. Looked up
            # dynamically — the host service provides ``_save_history``;
            # the worker mixin stays standalone-safe.
            save_history = getattr(self, "_save_history", None)
            if callable(save_history):
                track_task(asyncio.create_task(save_history()))

    async def _update_progress(self, item: DownloadItem, progress: Any) -> None:
        """Progress callback invoked from the store's ``install_game``.

        Stores report progress in two shapes:
        - Epic/Amazon pass a bare ``float`` (0.0-100.0), or a partial
          ``dict`` when one output line only carries some of the fields.
        - GOG/Ubisoft pass a ``dict`` with ``percentage``,
          ``downloaded_bytes``, ``total_bytes``, ``speed_bps``,
          ``eta_seconds``, ``phase``.

        Either way the merged item is what gets emitted, as ``item=`` —
        the same shape as every other ``DOWNLOAD_*`` event and byte-for-byte
        what ``get_download_queue`` hands the frontend, so the UI applies it
        by row id instead of guessing that it belongs to the visible row.
        Carrying the whole item also means ``download_phase`` /
        ``download_phase`` reaches the row on the progress tick rather than on
        the next queue refetch.
        """
        if isinstance(progress, (int, float)):
            item.progress = float(progress)
            if item.progress > 0:
                item.download_phase = "downloading"
        elif isinstance(progress, dict):
            apply_dict_progress(item, progress)
        if self._bus:
            from unifideck.core.types.events import Events

            await self._bus.emit(
                Events.DOWNLOAD_PROGRESS,
                item=item.to_dict(),
            )
