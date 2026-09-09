"""services/download/service.py — Central download queue + dispatcher.

Refactor of legacy download/manager.py. Queue accepting install
requests from the frontend, dispatching to the appropriate
``StoreBase`` via ``StoreRegistry``. Polymorphic — no per-store
branching; worker mixin handles the consumer loop.

Persists the queue so pending downloads survive plugin restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Result
from unifideck.launcher.wrapper_stores import uses_manual_download_phase

from .models import MAX_FINISHED_HISTORY, DownloadItem
from .persistence import load_queue, save_queue
from .validators import validate_path
from .warmup_runner import PrefixWarmupRunner
from .worker import _WorkerMixin

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)

# How long ``cancel`` waits for a cancelled install to unwind before
# answering. Long enough for the common case (a subprocess being reaped),
# short enough that the RPC always returns while the user is still looking
# at the button.
_CANCEL_SETTLE_S = 3.0

# Statuses that mean "this item is finished with". An item can still be sitting
# in ``_running`` while wearing one of these — the worker publishes the terminal
# event before its ``finally`` pops the entry — so the read boundary filters on
# status rather than trusting the dict membership.
_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled"})


def _newest_per_id(items: list[DownloadItem]) -> list[DownloadItem]:
    """Collapse to one entry per ``store:game_id``, keeping the last seen.

    Order is preserved at each id's *newest* position, which is what the
    Downloads page wants: the most recent outcome for a game, where that
    outcome happened.
    """
    keep: dict[str, DownloadItem] = {}
    for item in items:
        key = f"{item.store}:{item.game_id}"
        # Pop before setting so the entry moves to the end rather than
        # keeping its first-seen slot. ``get_queue`` reverses this list, so
        # position is recency: a retried game belongs where its newest
        # attempt happened. Matches what ``_cleanup_running`` does live.
        keep.pop(key, None)
        keep[key] = item
    return list(keep.values())


class DownloadService(_WorkerMixin):
    """Queue + dispatcher for store-agnostic game installations."""

    def __init__(
        self,
        bus: EventBus,
        registry: StoreRegistry,
        queue_file: str,
        max_concurrent: int = 1,
        launcher_path: str = "",
    ) -> None:
        """Store refs + queue path, init empty state."""
        self._bus = bus
        self._registry = registry
        self._queue_file = queue_file
        # Finished-history file — a sibling of the queue file (so it
        # lives under ~/.local/share/unifideck and survives plugin
        # reinstalls). Unlike the queue (pending items only), this
        # persists the "Recently finished" list across restarts.
        self._history_file = str(Path(queue_file).parent / "download_history.json")
        self._max_concurrent = max_concurrent
        self._launcher_path = launcher_path

        self._queue: list[DownloadItem] = []
        self._running: dict[str, DownloadItem] = {}
        # Mirror of ``_running`` keyed the same way, pointing at
        # the asyncio.Task driving each install. Lets ``cancel()``
        # actually kill a running download instead of returning
        # ``already_running`` — the worker catches CancelledError,
        # emits DOWNLOAD_CANCELLED, and runs its own cleanup.
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}
        # Post-install prefix setup. Separate from ``_running_tasks`` because
        # cancelling it means something different: the download has already
        # finished and been made durable, so a cancel here abandons the setup
        # and leaves the install standing. See ``warmup_runner``.
        self._warmup_runner = PrefixWarmupRunner()
        # Recent finished items (capped FIFO). Populated by the
        # worker's ``_cleanup_running``; surfaced to the frontend
        # via ``get_queue()["finished"]`` so the Downloads page can
        # show a history of completed / failed / cancelled installs.
        self._finished: list[DownloadItem] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None
        self._on_complete_callback: Any = None
        # Optional install-time prefix-warmup hook (see prefix_warmup.py). Set
        # via set_prefix_warmup during bootstrap; the worker runs it after a
        # successful install and before marking the item complete, for the
        # stores that own a per-game prefix (Epic / GOG / Amazon).
        self._prefix_warmup: Any = None

    async def start(self) -> None:
        """Load persisted queue + start the worker loop task."""
        if self._task is not None and not self._task.done():
            return

        await self._load_queue()
        await self._load_history()

        # Emit queued event for all items restored from disk
        if self._bus:
            from unifideck.core.types.events import Events

            for item in self._queue:
                await self._bus.emit(Events.DOWNLOAD_QUEUED, item=item.to_dict())

        self._task = asyncio.create_task(self._worker_loop())
        logger.info("[DownloadService] worker task started, %d items in queue", len(self._queue))

    async def stop(self) -> None:
        """Stop the worker loop — does NOT cancel running downloads.

        Cancels the worker task so new items won't dispatch;
        in-flight installs complete or fail on their own. Queue
        is persisted one last time to capture the final state.
        """
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("[DownloadService] worker task stopped")

        await self._save_queue()
        await self._save_history()

    async def add(
        self,
        store: str,
        game_id: str,
        install_path: str,
        title: str = "",
        is_update: bool = False,
        language: str | None = None,
        required_bytes: int | None = None,
        download_dir: str = "",
    ) -> Result:
        """Queue a new download request.

        ``is_update`` is recorded as-is on the item — the caller
        (the ``install_game`` vs ``update_game`` RPC) knows the
        operation; the service does not infer it.

        ``language`` is the user-picked install language (GOG
        multi-language games); recorded on the item and threaded
        to the store installer by the worker. ``None`` means "use
        the store default".

        ``required_bytes`` is the game's known download size (the
        caller resolves it — the service stays store-agnostic). When
        provided, the free-space preflight refuses a too-small target
        up front instead of failing deep inside the store CLI; ``None``
        (unknown size) falls back to the static floor.
        """
        # 1. Validation
        val_result = validate_path(install_path, required_bytes)
        if not val_result.success:
            return val_result
        if download_dir and download_dir != install_path:
            dl_result = validate_path(download_dir, required_bytes)
            if not dl_result.success:
                return dl_result

        key = f"{store}:{game_id}"

        async with self._lock:
            # 2. Duplicate check
            if key in self._running:
                return Result(success=False, error="already_running")

            for item in self._queue:
                if item.store == store and item.game_id == game_id:
                    return Result(success=False, error="already_queued")

            # 3. Add to queue
            item = DownloadItem(
                store=store,
                game_id=game_id,
                install_path=install_path,
                title=title,
                is_update=is_update,
                language=language or "",
                download_dir=download_dir,
                # Ubisoft is a launcher-driven (UPC) install with no real
                # download — mark it "manual" from enqueue so the UI shows the
                # indeterminate "Installing in <vendor client>" state instead
                # of a fake "Download Queued"/"DOWNLOADING 0%" bar, even while
                # it waits behind other downloads in the queue.
                download_phase=("manual" if uses_manual_download_phase(store) else "downloading"),
            )
            self._queue.append(item)

        # 4. Persist and emit outside the lock
        await self._save_queue()

        if self._bus:
            from unifideck.core.types.events import Events

            await self._bus.emit(Events.DOWNLOAD_QUEUED, item=item.to_dict())

        return Result(success=True)

    async def cancel(
        self,
        store: str,
        game_id: str,
    ) -> Result:
        """Cancel whatever this game currently has in flight.

        Five cases, in priority order:

        - **Post-install warmup** (in the warmup runner): abandon the prefix
          setup only. The download is already finished and durable.
        - **Already finished** (terminal status, still in ``_running``): there
          is nothing left to cancel, so report success and let the row clear.
          Cancelling here would be actively harmful — the worker is inside its
          finalisation, DOWNLOAD_COMPLETE has already gone out, and killing the
          task would flip a completed install to ``cancelled``.
        - **Running install** (in ``_running_tasks``): cancel the task; the
          worker's ``_run_install`` catches ``CancelledError``, marks the item,
          emits CANCELLED, and runs ``_cleanup_running`` in its finally block.
        - **Pending** (in ``_queue``): remove + emit CANCELLED.
        - **Orphan** (in ``_running`` with no live task): a row the frontend is
          still rendering that the backend has already finished with. Clear it
          instead of answering ``not_found``, which is what the user saw as
          "Cancel Failed / not_found" on a dead-but-visible row.

        Every outcome is logged. Nothing here used to log at all, which is why
        diagnosing that report needed a cross-check against on-disk state.
        """
        key = f"{store}:{game_id}"
        logger.info("[DownloadService] cancel requested for %s", key)
        if self._warmup_runner.cancel(key):
            logger.info(
                "[DownloadService] cancel %s: abandoned prefix warmup; "
                "the install stands", key,
            )
            return Result(success=True)
        active = self._running.get(key)
        if active is not None and active.status in _TERMINAL_STATUSES:
            logger.info(
                "[DownloadService] cancel %s: already %s, nothing to cancel",
                key, active.status,
            )
            return Result(success=True)
        if await self._cancel_running(key):
            logger.info("[DownloadService] cancel %s: running install cancelled", key)
            return Result(success=True)
        return await self._cancel_queued_or_orphan(store, game_id, key)

    async def _cancel_running(self, key: str) -> bool:
        """Cancel a live install task. False when there isn't one.

        The wait on the cancelled task is bounded. Cancellation is requested
        synchronously, but an installer sitting in a thread — ``zipfile``
        extraction, a large ``rmtree`` — only takes it at its next checkpoint,
        and this used to block for as long as that took. It is reached from an
        RPC handler: the frontend's cancel call would hang and be reported to
        the user as "cancel failed", for an install that was in fact being
        cancelled. Nothing here needs the unwind to finish: the worker emits
        ``DOWNLOAD_CANCELLED`` from its own handler, and its ``finally`` clears
        ``_running_tasks``.
        """
        running_task = self._running_tasks.get(key)
        if running_task is None or running_task.done():
            return False
        running_task.cancel()
        with contextlib.suppress(
            asyncio.CancelledError, asyncio.TimeoutError, Exception,
        ):
            await asyncio.wait_for(
                asyncio.shield(running_task), _CANCEL_SETTLE_S,
            )
        return True

    async def _cancel_queued_or_orphan(
        self, store: str, game_id: str, key: str,
    ) -> Result:
        """Drop a pending entry, or clear an orphaned running row."""
        async with self._lock:
            found_idx = -1
            for i, queued in enumerate(self._queue):
                if queued.store == store and queued.game_id == game_id:
                    found_idx = i
                    break
            if found_idx >= 0:
                item = self._queue.pop(found_idx)
                origin = "pending queue entry removed"
            else:
                orphan = self._running.pop(key, None)
                if orphan is None:
                    logger.info(
                        "[DownloadService] cancel %s: nothing in flight", key,
                    )
                    return Result(success=False, error="not_found")
                item = orphan
                origin = "cleared orphaned row (no live task)"
        item.status = "cancelled"
        await self._save_queue()
        if self._bus:
            from unifideck.core.types.events import Events

            await self._bus.emit(Events.DOWNLOAD_CANCELLED, item=item.to_dict())
        logger.info("[DownloadService] cancel %s: %s", key, origin)
        return Result(success=True)

    def get_queue(self) -> dict[str, Any]:
        """Return current state for the frontend.

        Keys match the frontend ``DownloadQueueInfo`` shape:
        ``current`` (active download or null), ``queued``
        (pending items), ``finished`` (history), ``state``
        (``"idle"`` / ``"running"``). Also keeps ``running``
        for backward compatibility with any internal consumers.

        A **terminal** item is never reported as active, even while it is still
        in ``_running``. The worker emits ``DOWNLOAD_COMPLETE`` before
        ``_cleanup_running`` pops the item, and the frontend refetches the
        queue the moment it sees that event — landing inside the gap and
        caching a row the backend was already done with. That gap is 243 ms on
        a GOG install and **6.5 s** on an Epic one, and since no further event
        follows, the stale row stuck permanently: an indeterminate "SETTING UP
        GAME..." whose Cancel could only ever answer ``not_found``. Filtering
        at the read boundary closes it regardless of timing, and keeps working
        if the ordering is ever changed again.

        ``add``'s ``already_running`` guard deliberately still reads raw
        ``_running``, so a duplicate install cannot be queued during that same
        gap.
        """
        running_items = [
            item
            for item in self._running.values()
            if item.status not in _TERMINAL_STATUSES
        ]
        current = running_items[0] if running_items else None
        # Newest-first: callers typically render the most recent
        # completion at the top of the Finished section.
        finished_items = list(reversed(self._finished))
        return {
            "current": current.to_dict() if current else None,
            "queued": [item.to_dict() for item in self._queue],
            "running": [item.to_dict() for item in running_items],
            "finished": [item.to_dict() for item in finished_items],
            "state": "running" if running_items else "idle",
        }

    async def clear_history(self, item_id: str | None = None) -> int:
        """Drop finished-download history entries. Returns how many went.

        The Downloads tab lists *installed games* now rather than a download
        log, so the only history rows it still surfaces are failures — which
        the user needs a way to dismiss once they've read them. ``item_id``
        clears one row; ``None`` clears them all. Purely a UI-history
        operation: nothing on disk or in Steam is touched.
        """
        before = len(self._finished)
        if item_id is None:
            self._finished = []
        else:
            # ``DownloadItem`` has no ``id`` field — the frontend's ``id`` is
            # derived in ``to_dict`` as ``"<store>:<game_id>"``, so rebuild it
            # here to match what the UI sends back.
            self._finished = [i for i in self._finished if f"{i.store}:{i.game_id}" != item_id]
        removed = before - len(self._finished)
        if removed:
            await self._save_history()
        return removed

    def set_on_complete_callback(self, callback: Any) -> None:
        """Register a post-install callback invoked by the worker.

        The callback receives the completed ``DownloadItem`` and
        should handle game registration (write ``.unifideck-id``
        marker, update ``games.map``, invalidate caches, trigger
        a sync reconcile, etc.).
        """
        self._on_complete_callback = callback

    def set_prefix_warmup(self, callback: Any) -> None:
        """Register the install-time prefix-warmup hook.

        The callback receives the completed ``DownloadItem`` and runs the full
        first-run prefix setup (createprefix + compat + cloud pull). The worker
        awaits it after a successful install and before marking the item
        complete, for the stores that own a per-game prefix. See
        ``prefix_warmup.make_prefix_warmup``.
        """
        self._prefix_warmup = callback

    async def _load_queue(self) -> None:
        """Replace in-memory queue with the persisted file."""
        try:
            self._queue = await load_queue(self._queue_file)
        except Exception as e:
            logger.warning("[DownloadService] failed to load queue, starting fresh: %s", e)
            self._queue = []

    async def _save_queue(self) -> None:
        """Flush in-memory queue to disk."""
        try:
            # Note: We only persist pending items, not running ones, because
            # a restart interrupts running installs anyway.
            await save_queue(self._queue_file, self._queue)
        except Exception as e:
            logger.warning("[DownloadService] failed to save queue: %s", e)

    async def _load_history(self) -> None:
        """Restore the recently-finished history from disk.

        Reuses the queue JSON codec (it's a generic ``list[DownloadItem]``).
        Capped to the most-recent ``MAX_FINISHED_HISTORY`` so a large
        on-disk file can't grow the in-memory list unbounded.

        Collapses duplicate ids on the way in, keeping the newest. Writers
        now keep one row per game, but a file written before that still
        holds a row per attempt, and those would otherwise render as
        repeated identical cards until the user cleared history by hand.
        """
        try:
            items = await load_queue(self._history_file)
            self._finished = _newest_per_id(items)[-MAX_FINISHED_HISTORY:]
        except Exception as e:
            logger.warning("[DownloadService] failed to load history, starting empty: %s", e)
            self._finished = []

    async def _save_history(self) -> None:
        """Persist the recently-finished history (most-recent N) to disk."""
        try:
            await save_queue(self._history_file, self._finished[-MAX_FINISHED_HISTORY:])
        except Exception as e:
            logger.warning("[DownloadService] failed to save history: %s", e)
