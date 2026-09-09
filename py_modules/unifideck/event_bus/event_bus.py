"""Event bus — core asynchronous pub/sub primitive.

``EventBus`` is the central pub/sub primitive of the plugin.
Every asynchronous interaction between services, RPC mixins and
stores flows through it.

Public API:

* ``on(event, handler)``      — register a persistent handler;
* ``once(event, handler)``    — register a one-shot handler that
  is auto-removed after the first emission;
* ``off(event, handler)``     — explicit unsubscribe;
* ``clear(event)``            — drop every handler for a given
  event, or every handler at all if ``event=None``;
* ``handler_count(event)``    — observability helper;
* ``emit(event, **payload)``  — fan-out to every subscriber,
  awaiting all in parallel.

The bus stores handlers as plain lists per event key (event
value or string). One-shot handlers are tracked separately in
``_once`` and pruned from ``_handlers`` after each emission.

Concurrency: ``emit`` schedules every handler concurrently via
``asyncio.gather(return_exceptions=True)`` so one slow handler
cannot block siblings, and one raising handler cannot abort the
emission for the others. Per-handler failures are logged at ERROR
but not propagated.

Sync handlers are supported via ``asyncio.to_thread`` so a
legacy callback that wasn't migrated to async still works
without blocking the event loop.

The core ``EventBus`` only implements basic subscribe/emit
semantics — circuit breaker, batching, replay, dead-letter queue
all live in sibling modules (``event_bus_reliability``,
``event_bus_scaling``, ``event_bus_extensions``, ``event_replay``)
and are wired together by ``BusPipeline``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from unifideck.core.types import Events

logger = logging.getLogger(__name__)
Handler = Callable[..., Awaitable[Any]] | Callable[..., Any]

# Ceiling for a single handler's execution inside ``emit``'s fan-out.
# ``SYNC_STARTED``/``SYNC_COMPLETE`` are emitted while ``SyncService``
# holds its single-flight ``_lock`` (see ``sync_run_mixin.py``/
# ``sync_finalize_mixin.py``) — without a bound here, one wedged
# subscriber (a stalled subprocess, an uncancellable socket call) hangs
# ``emit`` forever, which wedges the lock forever, and every future sync
# request queues behind it indefinitely (only a plugin/Steam restart,
# which rebuilds ``SyncService`` with a fresh ``Lock()``, recovers).
# 60s is generous for any legitimate handler (VDF reconcile, cache
# writes) while still surfacing a genuinely stuck handler in a bounded
# window instead of never (UD-013).
HANDLER_TIMEOUT_SECONDS = 60.0

class EventBus:
    """In-process async pub/sub with persistent + one-shot subscriptions."""

    def __init__(self) -> None:
        """Initialise empty subscriber tables.

        Two parallel dicts:

        * ``_handlers`` — the canonical ``event_key → [handler]``
          mapping; emissions iterate this list.
        * ``_once``     — subset of ``_handlers`` flagged for
          auto-removal after the next emission.
        """
        self._handlers: dict[str, list[Handler]] = {}
        self._once: dict[str, list[Handler]] = {}
        # Optional post-emit hook: when set, every ``emit`` call
        # also writes the event to the replay buffer so the
        # frontend's ``subscribe_replay`` polling sees it. Wired
        # by the bootstrap (after the replay buffer is built).
        # Direct injection (rather than going through the
        # PriorityDispatcher's enqueue) because every emitter in
        # the codebase calls ``bus.emit`` directly — the
        # dispatcher's queue was never fed.
        self._replay_recorder: Callable[[Any, dict[str, Any]], None] | None = None
        # Per-sync-run progress tracker, set by SyncService during
        # ``_setup_sync`` and cleared on completion. Services that
        # need to report per-game progress (ArtworkService,
        # MetadataService) read it via ``get_sync_progress()`` and
        # call its ``increment_*`` methods — no direct coupling to
        # SyncService needed.
        self._sync_progress: Any = None

    def set_sync_progress(self, progress: Any) -> None:
        """Store the active ``SyncProgress`` instance (or ``None``)."""
        self._sync_progress = progress

    def get_sync_progress(self) -> Any:
        """Return the current ``SyncProgress`` instance, or ``None``."""
        return self._sync_progress

    def set_replay_recorder(
        self, recorder: Callable[[Any, dict[str, Any]], None] | None,
    ) -> None:
        """Wire a post-emit hook that copies events to the replay buffer.

        Called once at boot from the pipeline factory with
        ``replay_buffer.record`` as the recorder. ``None`` clears
        the wiring (test teardown).

        Args:
            recorder: callable accepting ``(event, kwargs)`` — the
                same signature as ``EventReplayBuffer.record``.
                Set to ``None`` to disable.
        """
        self._replay_recorder = recorder

    def on(self, event: Events | str, handler: Handler) -> None:
        """Register a persistent handler for ``event``.

        Multiple subscribers can register for the same event;
        they're all invoked in registration order (insertion
        into the list). Duplicate registrations are allowed — the
        same handler will be invoked twice per emission. The
        caller is responsible for de-dup if that matters.

        Args:
            event: ``Events`` enum value or string equivalent.
            handler: sync or async callable. Sync ones run on a
                thread pool via ``asyncio.to_thread``.
        """
        key = self._key(event)
        self._handlers.setdefault(key, []).append(handler)
        logger.debug("[EventBus] on(%s) -> %s handlers", key, len(self._handlers[key]))

    def once(self, event: Events | str, handler: Handler) -> None:
        """Register a one-shot handler that's removed after the next emission.

        The handler is appended to both ``_handlers`` (so the
        emission finds it) and ``_once`` (so the post-emit
        pruning logic in ``emit`` knows to drop it). Useful for
        await-style patterns where a caller wants to react to
        the next occurrence and forget about it.

        Args:
            event: ``Events`` enum value or string equivalent.
            handler: sync or async callable.
        """
        key = self._key(event)
        self._handlers.setdefault(key, []).append(handler)
        self._once.setdefault(key, []).append(handler)
        logger.debug("[EventBus] once(%s)", key)

    def off(self, event: Events | str, handler: Handler) -> bool:
        """Remove a specific handler from ``event``'s subscriber list.

        Removes the handler from both ``_handlers`` and (if
        present) ``_once``. Identity-based: only the exact
        ``handler`` reference is removed, even if duplicates
        exist (only the first occurrence is removed per call).

        Args:
            event: ``Events`` enum value or string equivalent.
            handler: the callable to remove.

        Returns:
            ``True`` if the handler was found and removed,
            ``False`` if it wasn't registered (no-op).
        """
        key = self._key(event)
        if key not in self._handlers:
            return False
        try:
            self._handlers[key].remove(handler)
        except ValueError:
            return False
        if key in self._once and handler in self._once[key]:
            self._once[key].remove(handler)
        return True

    def clear(self, event: Events | None = None) -> None:
        """Drop every handler for one event, or for every event.

        Useful for test teardown where you want a fresh bus
        without rebuilding the instance, or for hot-reload paths
        that need to wipe stale subscriptions before re-wiring.

        Args:
            event: optional event to scope the clear to. ``None``
                (default) wipes every event's handlers and the
                ``_once`` table entirely.
        """
        if event is None:
            self._handlers.clear()
            self._once.clear()
            logger.debug("[EventBus] cleared all handlers")
        else:
            key = self._key(event)
            self._handlers.pop(key, None)
            self._once.pop(key, None)
            logger.debug("[EventBus] cleared %s", key)

    def unsubscribe_all(self, owner: object) -> int:
        """Remove every handler bound to ``owner``.

        Walks the full handler table and drops any handler whose
        ``__self__`` is the given object — i.e. every bound method
        of ``owner`` that was registered via :py:meth:`on` or
        :py:meth:`once`. Useful when a service shuts down and
        wants to detach from the bus without listing each
        ``off()`` call manually.

        The match is identity-based on ``__self__``; free
        functions and lambdas (no ``__self__``) are never
        matched. ``owner`` itself doesn't need to be the
        service class — any object whose bound methods were
        subscribed will be cleaned up.

        Args:
            owner: the object whose subscriptions to remove.

        Returns:
            The number of handlers removed across every event.
            Safe to call when ``owner`` has no subscriptions
            (returns 0).
        """
        removed = 0
        # Iterate over a snapshot of keys so we can mutate the
        # underlying dicts during iteration (deleting entries that
        # become empty after the sweep).
        for key in list(self._handlers.keys()):
            survivors = [
                h for h in self._handlers[key]
                if getattr(h, "__self__", None) is not owner
            ]
            removed += len(self._handlers[key]) - len(survivors)
            if survivors:
                self._handlers[key] = survivors
            else:
                del self._handlers[key]
            # Mirror the cleanup on the ``_once`` shadow table.
            once_list = self._once.get(key)
            if once_list is not None:
                survivors_once = [
                    h for h in once_list
                    if getattr(h, "__self__", None) is not owner
                ]
                if survivors_once:
                    self._once[key] = survivors_once
                else:
                    self._once.pop(key, None)
        logger.debug(
            "[EventBus] unsubscribe_all(%s) → %d handlers removed",
            type(owner).__name__, removed,
        )
        return removed

    def handler_count(self, event: Events | str) -> int:
        """Return the number of subscribers currently registered for ``event``.

        Used by tests (assert one or more subscribers exist
        before triggering an event) and by diagnostics (the
        QAM debug panel can show "no subscribers" warnings).

        Args:
            event: ``Events`` enum value or string equivalent.

        Returns:
            Subscriber count for that event; 0 if none.
        """
        return len(self._handlers.get(self._key(event), []))

    async def emit(self, event: Events | str, **payload: Any) -> list[Any]:
        """Fan-out an event to every registered handler in parallel.

        Pipeline:

        1. Snapshot the current handlers list (immune to
           concurrent mutations during the emission).
        2. Schedule every handler via ``_invoke`` (which
           dispatches sync vs async) and gather concurrently
           with ``return_exceptions=True``.
        3. After the gather completes, prune any one-shot
           handlers from the canonical ``_handlers`` list.
        4. Log per-handler exceptions at ERROR; the emission
           still returns the result list to the caller (with
           ``Exception`` instances in the slots that failed).
        5. Emit DIAG-level summary with total duration + success
           count.

        Args:
            event: ``Events`` enum value or string equivalent.
            **payload: forwarded to each handler as kwargs.

        Returns:
            List of per-handler results (or ``Exception``
            instances for failed handlers), in registration
            order. Empty list if no subscribers.
        """
        key = self._key(event)
        # Always record into the replay buffer (when wired), even
        # if no handlers are registered — the frontend may poll
        # for events before its components have mounted their
        # subscriptions. Recording is best-effort; a failing
        # recorder must never break emission.
        if self._replay_recorder is not None:
            try:
                self._replay_recorder(event, dict(payload))
            except Exception:
                logger.exception("[EventBus] replay recorder failed")
        handlers = list(self._handlers.get(key, []))
        if not handlers:
            logger.debug("[DIAG] event=%s handlers=0", key)
            return []
        started = time.monotonic()
        logger.debug(
            "[DIAG] event=%s handlers=%d payload_keys=%s",
            key,
            len(handlers),
            list(payload.keys()),
        )
        tasks = [self._invoke(h, payload) for h in handlers]
        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )
        once_list = self._once.get(key, [])
        if once_list:
            remaining = [h for h in self._handlers[key] if h not in once_list]
            self._handlers[key] = remaining
            self._once[key] = []
        dt_total = (time.monotonic() - started) * 1000
        ok = sum(1 for r in results if not isinstance(r, Exception))
        self._log_handler_failures(key, handlers, results)
        logger.debug(
            "[DIAG] event=%s total=%.2fms success=%d/%d",
            key,
            dt_total,
            ok,
            len(results),
        )
        return results

    @staticmethod
    def _log_handler_failures(
        key: str, handlers: list[Handler], results: list[Any],
    ) -> None:
        """Report per-handler failures from one fan-out.

        Split out of ``emit`` when the quarantine branch pushed it past the
        80-line cap. Volumetry rule: an over-cap function is split, never
        allowlisted.
        """
        for i, r in enumerate(results):
            if not isinstance(r, Exception):
                continue
            name = getattr(handlers[i], "__qualname__", repr(handlers[i]))
            # A quarantined handler is a deliberate skip, not a crash: the
            # watchdog refused to run it after repeated timeouts and the
            # other handlers still ran. Logging it at ERROR would train
            # readers to ignore the line that reports real failures
            # (audit register item 4g).
            if type(r).__name__ == "HandlerQuarantinedError":
                logger.warning(
                    "[EventBus] handler #%d (%s) for %s SKIPPED — "
                    "quarantined by the watchdog",
                    i, name, key,
                )
                continue
            logger.error(
                "[EventBus] handler #%d (%s) for %s failed: %s: %s",
                i, name, key, type(r).__name__, r,
            )

    async def _invoke(self, handler: Handler, payload: dict[str, Any]) -> Any:
        """Dispatch one handler call, awaiting it directly or on a thread.

        ``inspect.iscoroutinefunction`` checks at call time
        because handlers might be wrapped (e.g. by the dev-ex
        decorators) — the check via the function attribute
        ``__call__`` would miss those cases.

        Bounded by ``HANDLER_TIMEOUT_SECONDS`` so one wedged handler
        can't hang ``emit`` (and, transitively, any lock the emitting
        caller holds — see the constant's docstring) forever. A
        timeout is returned like any other per-handler failure: the
        caller's ``asyncio.gather(..., return_exceptions=True)`` sees
        a ``TimeoutError`` in this slot and logs it, but ``emit``
        itself still returns on schedule.

        Args:
            handler: the callable to invoke.
            payload: kwargs to forward.

        Returns:
            The handler's return value (after await for async
            handlers, after thread completion for sync ones).
        """
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            return await self._invoke_supervised(watchdog, handler, payload)

        if inspect.iscoroutinefunction(handler):
            return await asyncio.wait_for(
                handler(**payload), timeout=HANDLER_TIMEOUT_SECONDS,
            )
        return await asyncio.wait_for(
            asyncio.to_thread(handler, **payload),
            timeout=HANDLER_TIMEOUT_SECONDS,
        )

    async def _invoke_supervised(
        self, watchdog: Any, handler: Handler, payload: dict[str, Any],
    ) -> Any:
        """Run one handler under ``HandlerWatchdog`` supervision.

        The watchdog holds the per-handler timeout, the consecutive-timeout
        count and the quarantine escalation. Until 2026-08-26 its ``invoke``
        had **zero callers** while ``priority_dispatcher`` claimed the
        opposite in a comment — "invocation goes through the bus directly
        (the bus uses the watchdog internally)" — which was simply untrue.
        Audit register item 4g.

        Two details the watchdog's own ``invoke`` does not handle, which is
        why this wrapper exists rather than a direct delegation:

        * **Sync handlers.** ``watchdog.invoke`` awaits ``handler(...)``
          directly. A plain function would return a non-awaitable and raise.
          Passing a thunk that returns ``asyncio.to_thread(...)`` keeps sync
          handlers on a worker thread exactly as before.
        * **Quarantine.** A quarantined handler raises
          ``HandlerQuarantinedError`` instead of running. ``emit`` gathers
          with ``return_exceptions=True``, so it lands in that handler's slot
          like any other failure and the other handlers still run — which is
          the intended behaviour, but it must not be logged as a crash.

        Falls back to the unsupervised path if the watchdog is a stub without
        ``invoke`` (the tests build bare objects), so supervision can never
        stop an event being delivered.
        """
        invoke = getattr(watchdog, "invoke", None)
        if invoke is None:
            if inspect.iscoroutinefunction(handler):
                return await asyncio.wait_for(
                    handler(**payload), timeout=HANDLER_TIMEOUT_SECONDS,
                )
            return await asyncio.wait_for(
                asyncio.to_thread(handler, **payload),
                timeout=HANDLER_TIMEOUT_SECONDS,
            )

        name = getattr(handler, "__self__", None)
        qualname = (
            f"{type(name).__name__}.{handler.__name__}"
            if name is not None and hasattr(handler, "__name__")
            else getattr(handler, "__qualname__", repr(handler))
        )
        if inspect.iscoroutinefunction(handler):
            return await invoke(qualname, handler, **payload)
        return await invoke(
            qualname,
            lambda **kw: asyncio.to_thread(handler, **kw),
            **payload,
        )

    @staticmethod
    def _key(event: Events | str) -> str:
        """Normalise event identifiers to a canonical string key.

        ``Events`` enum members are mapped to their ``.value``;
        anything else is coerced via ``str()``. Used by every
        public method that accepts an event so internal storage
        is consistent (single string-keyed dict).

        Args:
            event: ``Events`` enum value or any string.

        Returns:
            The canonical string form used as the dict key.
        """
        if isinstance(event, Events):
            # Cast through ``str`` to anchor the return type — StrEnum's
            # ``.value`` is typed as the enum's underlying type which
            # mypy widens to Any in some import paths.
            return str(event.value)
        return str(event)
