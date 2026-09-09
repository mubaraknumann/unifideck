"""Priority dispatcher — async priority queue + coalescing on top of EventBus.

``PriorityDispatcher`` wraps a plain ``EventBus`` with three
extra capabilities:

* **Priority ordering** — events are stored in an
  ``asyncio.PriorityQueue`` keyed on ``(priority, seq)`` so
  ``CRITICAL`` events are delivered before ``NORMAL`` before
  ``BACKGROUND``, with FIFO ordering inside each tier.

* **Coalescing** — when an event arrives with a coalesce key
  (from ``event_priority.get_coalesce_key``) that already has
  a pending item in the queue, the older item is marked
  ``dropped=True`` and the new one takes its place. Useful for
  ``DOWNLOAD_PROGRESS``: a burst of progress updates collapses
  to "only the latest".

* **Backpressure** — when the pending ``BACKGROUND`` queue
  reaches ``background_cap`` (default 500), new BACKGROUND
  events are refused rather than queued. CRITICAL and NORMAL
  events are never dropped.

The dispatcher runs a single background worker task
(``_worker``) that pops items in priority order and forwards
them to the underlying bus via ``emit``. Optional hooks at
each stage:

* ``latency_collector`` — records per-event dispatch latency;
* ``replay_buffer``     — pushes the event into the replay
  ring for backfill;
* ``batch_dispatcher``  — groups coalescible events into
  ``<event>_batch`` deliveries instead of one-by-one.

Public API: ``start`` / ``stop`` / ``enqueue`` / ``get_metrics``.
``_QueueItem`` is the typed record stored in the heap;
``DispatcherMetrics`` is the observability snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events

from .event_priority import (
    EventPriority,
    get_coalesce_key,
    get_priority,
)

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .event_bus_scaling import BatchDispatcher
    from .event_replay import EventReplayBuffer
    from .supervision.watchdog_handler import HandlerWatchdog

logger = logging.getLogger(__name__)

DEFAULT_BACKGROUND_CAP = 500
DROP_WARNING_INTERVAL_SEC = 60.0

@dataclass(order=True)
class _QueueItem:
    """One queued event waiting for dispatch.

    The ``order=True`` + first two fields make this dataclass
    sortable by ``(priority, seq)`` — exactly what
    ``asyncio.PriorityQueue`` needs. The other fields are
    flagged ``compare=False`` so they don't affect ordering.

    Attributes:
        priority: numeric priority from ``EventPriority``
            (lower is higher). Used as the primary sort key.
        seq: monotonic sequence number for FIFO ordering
            within a priority tier.
        event: the event identifier (``Events`` member or
            string), or ``None`` for the sentinel that wakes
            the worker on shutdown.
        kwargs: the event payload to forward to subscribers.
        dropped: set to ``True`` when the item has been
            coalesced away — the worker skips it instead of
            dispatching.
    """

    priority: int
    seq: int
    event: Events | str | None = field(compare=False)
    kwargs: dict[str, Any] = field(compare=False)
    dropped: bool = field(default=False, compare=False)

@dataclass
class DispatcherMetrics:
    """Observability counters maintained by the dispatcher.

    Attributes:
        emitted_total: every call to ``enqueue``, even drops
            and coalesces.
        dispatched_total: items that actually reached the bus
            (not dropped, not coalesced away).
        coalesced_total: items that were superseded by a later
            same-key emission.
        dropped_background_total: BACKGROUND items refused due
            to saturation.
        pending_by_priority: live snapshot computed in
            ``get_metrics`` (so it reflects "right now" rather
            than a stale counter).
    """

    emitted_total: int = 0
    dispatched_total: int = 0
    coalesced_total: int = 0
    dropped_background_total: int = 0
    pending_by_priority: dict[str, int] = field(
        default_factory=lambda: {"CRITICAL": 0, "NORMAL": 0, "BACKGROUND": 0},
    )

class PriorityDispatcher:
    """Priority queue + coalescing + backpressure in front of an EventBus."""

    def __init__(
        self,
        bus: EventBus,
        *,
        background_cap: int = DEFAULT_BACKGROUND_CAP,
        watchdog: HandlerWatchdog | None = None,
        latency_collector: Any = None,
        replay_buffer: EventReplayBuffer | None = None,
        batch_dispatcher: BatchDispatcher | None = None,
    ) -> None:
        """Wire the dispatcher to its bus + optional supervision hooks.

        All hooks are optional — the dispatcher works with the
        bus alone. They're typically wired by ``BusPipeline``.

        Args:
            bus: the underlying ``EventBus`` to forward to.
            background_cap: maximum pending BACKGROUND items
                before new ones are refused (default 500).
            watchdog: optional ``HandlerWatchdog``. Stored here for
                the health snapshot; the supervision itself happens
                in ``EventBus._invoke_supervised``, which reads the
                watchdog off the bus. This parameter's docstring
                claimed "the bus uses the watchdog internally" from
                the start — untrue until 2026-08-26, when the bus
                was actually wired to it (audit register item 4g).
                Until then nothing called ``watchdog.invoke`` and it
                tracked zero handlers.
            latency_collector: optional sink for per-event
                dispatch latencies.
            replay_buffer: optional ring buffer to record
                dispatched events for backfill.
            batch_dispatcher: optional batching layer for
                coalescible events.
        """
        self._bus = bus
        self._background_cap = background_cap
        self._watchdog = watchdog
        self._latency = latency_collector
        self._replay = replay_buffer
        self._batcher = batch_dispatcher
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        self._coalesce_map: dict[tuple[str, str], _QueueItem] = {}
        self._seq = 0
        self._metrics = DispatcherMetrics()
        self._last_drop_warn: float = 0.0
        self._worker_task: asyncio.Task[Any] | None = None
        self._stopping = False

    async def start(self) -> None:
        """Spawn the worker task that drains the priority queue.

        Idempotent: a re-call when the worker is already
        running is a no-op. Resets ``_stopping`` to ``False``
        first to allow restarts after a ``stop``.
        """
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopping = False
        self._worker_task = asyncio.create_task(
            self._worker(),
            name="priority-dispatcher",
        )

    async def stop(self) -> None:
        """Signal the worker to exit and await its termination.

        Flips ``_stopping`` to ``True`` and pushes a sentinel
        item (``seq=-1``, ``event=None``) into the queue so
        the worker wakes from its ``await self._queue.get()``
        promptly. Then awaits the worker with a 5 s timeout;
        if exceeded, cancels the task forcefully.

        Safe to call multiple times — subsequent calls just
        re-set state without effect.
        """
        self._stopping = True
        if self._worker_task is None:
            return
        await self._queue.put(
            _QueueItem(
                priority=int(EventPriority.CRITICAL),
                seq=-1,
                event=None,
                kwargs={},
            ),
        )
        try:
            await asyncio.wait_for(self._worker_task, timeout=5.0)
        except TimeoutError:
            logger.warning(
                "[PriorityDispatcher] worker did not stop in 5s — cancelling",
            )
            self._worker_task.cancel()
        self._worker_task = None

    def enqueue(
        self,
        event: Events | str,
        *,
        priority: EventPriority | None = None,
        **kwargs: Any,
    ) -> bool:
        """Add an event to the priority queue.

        Three-step decision:

        1. **Saturation check** — if BACKGROUND queue full,
           drop and return ``False``.
        2. **Coalescence check** — if a same-coalesce-key item
           is already pending, mark it dropped and push the
           new one (which inherits the older's queue position
           through coalescing, just with the latest payload).
        3. **Normal push** — append to the heap.

        Args:
            event: the event identifier.
            priority: optional explicit priority override.
                ``None`` (default) uses
                ``get_priority(event)``.
            **kwargs: event payload.

        Returns:
            ``True`` on accept (queued or coalesced), ``False``
            on drop (BACKGROUND-only saturation case).
        """
        self._metrics.emitted_total += 1
        prio = priority if priority is not None else get_priority(event)
        if self._is_saturated(prio):
            self._record_drop()
            return False
        if self._coalesce_if_possible(event, prio, kwargs):
            return True
        self._push(event, prio, kwargs)
        return True

    def get_metrics(self) -> DispatcherMetrics:
        """Return the metrics record with a live pending-count snapshot.

        Walks the internal queue (cheap: O(N) over a deque)
        to count pending items per priority tier, skipping
        ``dropped`` ones (already coalesced away). The counters
        on the returned record are mutated in place — the
        same ``DispatcherMetrics`` instance is reused across
        calls.

        Returns:
            The shared ``DispatcherMetrics`` instance.
        """
        pending = {"CRITICAL": 0, "NORMAL": 0, "BACKGROUND": 0}
        for item in list(self._queue._queue):  # type: ignore[attr-defined]  # PriorityQueue._queue is used to introspect heap contents
            if item.dropped:
                continue
            name = EventPriority(item.priority).name
            pending[name] = pending.get(name, 0) + 1
        self._metrics.pending_by_priority = pending
        return self._metrics

    def _is_saturated(self, prio: EventPriority) -> bool:
        """Return whether new BACKGROUND items should be dropped.

        Non-BACKGROUND emissions are never refused (CRITICAL
        and NORMAL stay unbounded). For BACKGROUND, walks the
        queue and counts undropped BACKGROUND items; returns
        ``True`` once the count reaches ``background_cap``.

        Args:
            prio: priority of the incoming emission.

        Returns:
            ``True`` only when ``prio == BACKGROUND`` and the
            pending BACKGROUND count is at or above the cap.
        """
        if prio != EventPriority.BACKGROUND:
            return False
        pending_bg = sum(
            not item.dropped and item.priority == int(EventPriority.BACKGROUND)
            for item in list(self._queue._queue)  # type: ignore[attr-defined]  # PriorityQueue._queue used to introspect heap contents
        )
        return pending_bg >= self._background_cap

    def _record_drop(self) -> None:
        """Bump the drop counter and rate-limit the WARN log.

        Logs at WARN at most once every
        ``DROP_WARNING_INTERVAL_SEC`` (60 s) to avoid log spam
        on sustained saturation; every drop is still logged at
        DEBUG so detailed traces capture the full count.

        Mutates ``self._last_drop_warn`` to track the rate
        limit.
        """
        self._metrics.dropped_background_total += 1
        now = time.monotonic()
        if now - self._last_drop_warn >= DROP_WARNING_INTERVAL_SEC:
            self._last_drop_warn = now
            logger.warning(
                "[PriorityDispatcher] BACKGROUND queue saturated — "
                "dropped %d events total (cap=%d)",
                self._metrics.dropped_background_total,
                self._background_cap,
            )
        logger.debug(
            "[PriorityDispatcher] drop #%d",
            self._metrics.dropped_background_total,
        )

    def _coalesce_if_possible(
        self,
        event: Events | str,
        prio: EventPriority,
        kwargs: dict[str, Any],
    ) -> bool:
        """Try to coalesce ``event`` against a same-key pending item.

        Pipeline:

        1. Resolve the coalesce key for this event (e.g.
           ``"download_id"`` for ``DOWNLOAD_PROGRESS``).
        2. If no key or the key isn't in ``kwargs`` →
           non-coalescible, return ``False``.
        3. Compute the coalesce-map key
           ``(event_str, str(value))``.
        4. If a pending entry exists and isn't already
           dropped, mark it dropped and push a fresh item
           with the new payload (the new one inherits the
           older's queue position via the coalesce map
           update inside ``_push``).

        Args:
            event: event identifier.
            prio: resolved priority.
            kwargs: payload.

        Returns:
            ``True`` if a coalesce happened, ``False`` if the
            caller should perform a normal push.
        """
        key_name = get_coalesce_key(event)
        if not key_name or key_name not in kwargs:
            return False
        event_str = event.value if isinstance(event, Events) else str(event)
        coalesce_map_key = (event_str, str(kwargs[key_name]))
        existing = self._coalesce_map.get(coalesce_map_key)
        if existing is None or existing.dropped:
            return False
        existing.dropped = True
        self._metrics.coalesced_total += 1
        self._push(event, prio, kwargs, coalesce_map_key)
        return True

    def _push(
        self,
        event: Events | str,
        prio: EventPriority,
        kwargs: dict[str, Any],
        coalesce_map_key: tuple[str, str] | None = None,
    ) -> None:
        """Build a ``_QueueItem``, queue it, and update the coalesce map.

        Increments the global sequence counter so FIFO
        ordering within a priority tier is preserved. If the
        item is coalescible (or already had its key resolved
        by ``_coalesce_if_possible``), updates the
        ``coalesce_map`` so the next emission with the same
        key can find it.

        Args:
            event: event identifier.
            prio: resolved priority.
            kwargs: payload.
            coalesce_map_key: pre-computed coalesce key from
                ``_coalesce_if_possible``, or ``None`` to
                resolve inline.
        """
        self._seq += 1
        item = _QueueItem(
            priority=int(prio),
            seq=self._seq,
            event=event,
            kwargs=kwargs,
        )
        self._queue.put_nowait(item)
        if coalesce_map_key is None:
            key_name = get_coalesce_key(event)
            if key_name and key_name in kwargs:
                event_str = event.value if isinstance(event, Events) else str(event)
                coalesce_map_key = (event_str, str(kwargs[key_name]))
        if coalesce_map_key is not None:
            self._coalesce_map[coalesce_map_key] = item

    async def _worker(self) -> None:
        """Main loop: pop in priority order and dispatch to the bus.

        Loop invariants:

        * Items with ``dropped=True`` are skipped silently
          (coalesced or otherwise-superseded).
        * The sentinel item (``seq=-1`` + ``_stopping``) exits
          the loop cleanly.
        * Dispatch errors are routed to
          ``_handle_dispatch_error`` rather than crashing the
          worker — the queue keeps draining.
        * Every popped item calls ``task_done`` so a
          ``join``-style waiter can know when the queue is
          empty.
        """
        while not self._stopping:
            item = await self._queue.get()
            try:
                if self._stopping and item.seq == -1:  # type: ignore[unreachable]  # monotonic priority fallback
                    return  # type: ignore[unreachable]  # monotonic priority fallback
                if item.dropped:
                    continue
                await self._dispatch_one(item)
            except Exception as e:
                self._handle_dispatch_error(item, e)
            finally:
                self._queue.task_done()

    async def _dispatch_one(self, item: _QueueItem) -> None:
        """Forward one item to the bus, recording metrics + replay.

        Two delivery paths:

        * **Coalescible event with batcher attached** —
          accumulate in the ``BatchDispatcher`` and flush as
          ``<event>_batch`` when ready (with the batched
          payload list).
        * **Default path** — call ``self._bus.emit`` directly
          with the payload.

        Side effects after the bus call:

        * Bump ``dispatched_total``;
        * Record latency in ``_latency`` (if configured);
        * Record the event in ``_replay`` (if configured).

        Args:
            item: the dequeued item.
        """
        import time

        if item.event is None:
            return
        event_str = (
            item.event.value if isinstance(item.event, Events) else str(item.event)
        )
        t0 = time.monotonic()
        if self._batcher is not None and get_coalesce_key(item.event):
            should_flush = self._batcher.add(event_str, item.kwargs)
            if should_flush:
                batch = self._batcher.drain(event_str)
                await self._bus.emit(
                    f"{event_str}_batch",
                    batch=batch,
                )
        else:
            await self._bus.emit(item.event, **item.kwargs)
        duration_ms = (time.monotonic() - t0) * 1000
        self._metrics.dispatched_total += 1
        if self._latency is not None:
            self._latency.record(event_str, duration_ms)
        if self._replay is not None:
            self._replay.record(item.event, item.kwargs)

    def _handle_dispatch_error(self, item: _QueueItem, err: Exception) -> None:
        """Log a dispatch error and keep the worker running.

        Exceptions at this layer are unusual (the bus's own
        ``emit`` catches per-handler failures and returns
        results). When one does reach here, it's typically a
        bug in the dispatcher wiring itself — log loudly
        (``exception`` includes the traceback) but don't crash
        the worker so other events can still be processed.

        Args:
            item: the item that triggered the error.
            err: the caught exception.
        """
        logger.exception(
            "[PriorityDispatcher] handler error on %s: %s",
            item.event,
            err,
        )
