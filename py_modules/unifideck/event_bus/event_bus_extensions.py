"""Four small bus extensions grouped to minimise file proliferation.

Each extension is a standalone class — SRP is preserved at the
class level even though they share a file:

* **TypedEventRegistry** (P7.4) — runtime validation of event
  kwargs against a declared schema, with ``EventPayload``
  Protocol stubs for mypy.
* **DeadLetterQueue** (P7.5) — bounded ring buffer of events
  whose handlers all failed, for post-mortem inspection.
* **PredicateFilter** (P7.6) — wrap a handler with an
  arbitrary pre-invocation filter (e.g. only fire for a
  specific store).
* **DebugSnapshot** (P7.7) — dev-only state dump aggregating
  bus, dispatcher, watchdog, metrics, replay and DLQ for
  bug tickets.

Security note: the DLQ keeps payloads in memory only; if a
caller later persists it (not done by default), the file should
be ``0o600`` because payloads may contain sensitive kwargs like
store names or game IDs. OAuth tokens must never be passed as
event kwargs — this is a callsite responsibility the DLQ cannot
enforce.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
)

from unifideck.core.types import Events

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .event_replay import EventReplayBuffer
    from .priority_dispatcher import PriorityDispatcher
    from .supervision.metrics_handler import HandlerLatencyCollector
    from .supervision.watchdog_handler import HandlerWatchdog
logger = logging.getLogger(__name__)

# ── P7.4 — Typed event schemas ───────────────────────────────────
class EventPayload(Protocol):
    """Marker Protocol for typed event payloads.
    Concrete payloads inherit from this via Protocol subclassing:
                    class SyncCompletePayload(EventPayload, Protocol):
                                    games: list
                                    stores_synced: list
                                    duration_ms: int
    """

@dataclass
class EventSchema:
    """Declarative kwargs contract for one event type.

    Attributes:
        required: set of kwarg names that **must** be present.
            Missing any of these triggers a validation error.
        optional: set of kwarg names that **may** be present.
            Combined with ``required``, these define the
            complete allow-list; kwargs outside the union are
            flagged as unexpected.
    """

    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)

    def validate(self, kwargs: dict[str, Any]) -> str | None:
        """Check that ``kwargs`` matches this schema's contract.

        Two checks in order:

        1. Every name in ``required`` must be present in
           ``kwargs`` — first miss returns a "missing required"
           message.
        2. Every name in ``kwargs`` must be in
           ``required | optional`` — first surplus returns an
           "unexpected kwargs" message.

        Both messages include sorted name lists so they're
        stable across runs.

        Args:
            kwargs: payload dict to validate.

        Returns:
            ``None`` if the payload is valid, else an
            error string describing the first problem found.
        """
        missing = self.required - set(kwargs.keys())
        if missing:
            return f"missing required kwargs: {sorted(missing)}"
        allowed = self.required | self.optional
        extra = set(kwargs.keys()) - allowed
        if extra:
            return f"unexpected kwargs: {sorted(extra)}"
        return None

class TypedEventRegistry:
    """Holds per-event schemas and validates at emit time.

    Used by the bus pipeline as an optional pre-flight check:
    when an emitter passes a kwargs dict that violates the
    declared schema (missing required key, or unexpected
    extras), the registry returns an error string and the
    emission is rejected (or just logged, depending on
    configuration).
    """

    def __init__(self) -> None:
        """Initialise an empty schema registry.

        Events without a declared schema pass validation by
        default — registering schemas is opt-in per event.
        """
        self._schemas: dict[str, EventSchema] = {}

    def declare(self, event: Events | str, schema: EventSchema) -> None:
        """Register a schema for ``event``.

        Idempotent: re-declaring an event replaces the prior
        schema. The event identifier is normalised to its
        string form (the ``Events.value``) so subsequent
        validations can use either form.

        Args:
            event: ``Events`` enum value or string equivalent.
            schema: the contract (required + optional kwargs)
                this event must satisfy.
        """
        key = event.value if isinstance(event, Events) else str(event)
        self._schemas[key] = schema

    def validate(self, event: Events | str, kwargs: dict[str, Any]) -> str | None:
        """Validate ``kwargs`` against the registered schema for ``event``.

        Unregistered events pass through (return ``None``) —
        the registry deliberately doesn't enforce a "must
        declare every event" policy, so new event types can be
        introduced without breaking existing emissions.

        Args:
            event: ``Events`` enum value or string equivalent.
            kwargs: the payload to validate.

        Returns:
            ``None`` on success (or unregistered event), or an
            error message string describing what's wrong.
        """
        key = event.value if isinstance(event, Events) else str(event)
        schema = self._schemas.get(key)
        if schema is None:
            return None  # unregistered events pass through
        return schema.validate(kwargs)

# ── P7.6 — Predicate filter on subscriptions ─────────────────────
# A predicate is any callable taking kwargs and returning bool.
Predicate = Callable[..., bool]

class PredicateFilter:
    """Wraps a handler with an arbitrary pre-invocation filter.

    Usage at subscription time::

        filter = PredicateFilter(
            handler, lambda store, **_: store == "epic"
        )
        bus.on(Events.GAME_LAUNCHED, filter)

    The wrapped handler only runs when the predicate returns
    truthy. The filter forwards ``__name__`` and ``__qualname__``
    from the inner handler so the watchdog and latency
    collector see meaningful identifiers (not "filtered_handler"
    or some opaque wrapper).
    """

    def __init__(self, handler: Callable[..., Any], predicate: Predicate) -> None:
        """Wrap ``handler`` with the gate predicate ``predicate``.

        Pulls the handler's name and qualified name onto self
        so introspection-based machinery (watchdog,
        latency collector) sees the inner handler, not the
        wrapper.

        Args:
            handler: the underlying coroutine function to
                invoke when the predicate matches.
            predicate: any callable taking the event's kwargs
                and returning a bool. Sync only (called
                inline, not awaited).
        """
        self._handler = handler
        self._predicate = predicate
        # Preserve the inner handler name for watchdog/metrics
        self.__name__ = getattr(handler, "__name__", "filtered_handler")
        self.__qualname__ = getattr(handler, "__qualname__", self.__name__)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Run the predicate, then invoke the wrapped handler if it matched.

        Defensive: a predicate that raises is logged at
        EXCEPTION and treated as a pass-through (predicate
        returned ``True``). Rationale: a broken predicate
        eating events silently would be much harder to debug
        than a verbose log + duplicate delivery — and the
        wrapped handler can defend itself.

        Returns ``None`` when the predicate refuses the event,
        else whatever the inner handler returns.

        Args:
            *args / **kwargs: forwarded to both the predicate
                and the handler.

        Returns:
            ``None`` on predicate refusal, otherwise the inner
            handler's return value.
        """
        try:
            matches = self._predicate(*args, **kwargs)
        except Exception:
            # A broken predicate should not silently eat events —
            # log and pass through so the handler still runs.
            logger.exception(
                "[PredicateFilter] predicate raised; passing through",
            )
            matches = True
        if not matches:
            return None
        return await self._handler(*args, **kwargs)

# ── P7.7 — Debug snapshot ────────────────────────────────────────
class DebugSnapshot:
    """Collects the full state of bus + dispatcher for debugging.
    Called by a dev-only RPC `debug_snapshot()` on the Plugin class.
    Returns a JSON-serializable dict that operators can paste into
    bug tickets to reproduce issues. Never called in production hot
    paths — cost is measured once per call, not per event.
    """

    @staticmethod
    def collect(
        bus: EventBus,
        dispatcher: PriorityDispatcher | None = None,
        watchdog: HandlerWatchdog | None = None,
        metrics: HandlerLatencyCollector | None = None,
        replay: EventReplayBuffer | None = None,
        dlq: DeadLetterQueue | None = None,
    ) -> dict[str, Any]:
        """Gather every observable slice of state into one dict.

        Each optional collaborator is queried only if present —
        an environment without a dispatcher (e.g. a stripped-
        down test) still produces a valid snapshot, just with
        fewer sections.

        The output shape is intentionally flat-ish and JSON-
        serialisable: it's meant to be pasted into bug reports
        and parsed by external tooling without surprises.

        Args:
            bus: required; the underlying bus.
            dispatcher: optional priority dispatcher.
            watchdog: optional handler watchdog.
            metrics: optional latency collector.
            replay: optional replay buffer.
            dlq: optional dead-letter queue.

        Returns:
            Nested dict with sections per available collaborator
            (always ``bus``; conditionally ``dispatcher``,
            ``watchdog``, ``handler_metrics``, ``replay_sizes``,
            ``dlq_entries``).
        """
        snapshot: dict[str, Any] = {
            "bus": {
                "handler_counts": DebugSnapshot._safe_call(
                    getattr(bus, "health", None),
                ),
            },
        }
        if dispatcher is not None:
            m = dispatcher.get_metrics()
            snapshot["dispatcher"] = {
                "emitted_total": m.emitted_total,
                "dispatched_total": m.dispatched_total,
                "coalesced_total": m.coalesced_total,
                "dropped_background_total": m.dropped_background_total,
                "pending_by_priority": m.pending_by_priority,
            }
        if watchdog is not None:
            snapshot["watchdog"] = {
                name: {
                    "invocations": ms.invocations,
                    "timeouts": ms.timeouts,
                    "consecutive_timeouts": ms.consecutive_timeouts,
                    "quarantined": ms.quarantined,
                }
                for name, ms in watchdog.get_metrics().items()
            }
        if metrics is not None:
            snapshot["handler_metrics"] = metrics.get_top_n(n=10)
        if replay is not None:
            snapshot["replay_sizes"] = {
                "total": replay.size(),
            }
        if dlq is not None:
            snapshot["dlq_entries"] = len(dlq)
        return snapshot

    @staticmethod
    def _safe_call(fn: Callable[..., Any] | None) -> Any:
        """Call ``fn`` if present and isolate its exceptions.

        Helper for ``collect`` so a misbehaving collaborator
        method (e.g. ``bus.health`` raising) doesn't break the
        whole snapshot. Exceptions are converted to a small
        ``{"error": ...}`` dict in the output so the caller
        can still parse the snapshot and see what went wrong.

        Args:
            fn: callable or ``None``.

        Returns:
            ``None`` if ``fn`` is ``None``;
            the call result if it succeeds;
            ``{"error": str(e)}`` if it raises.
        """
        if fn is None:
            return None
        try:
            return fn()
        except Exception as e:
            return {"error": str(e)}

# ── P7.5 — Dead letter queue ─────────────────────────────────────
class DeadLetterQueue:
    """Capture events whose handlers all failed.
    When an event is emitted and ZERO of its registered handlers
    complete successfully (every one raises or times out), the
    event is appended to the dead letter queue so operators can
    inspect it later rather than losing the payload silently.
    Kept deliberately minimal: a bounded ring buffer of (event,
    payload, reason) tuples. Production callers either drain
    the queue for a diagnostics RPC, or log-and-forget on
    shutdown. There is no retry mechanism — DLQ is for audit,
    not for recovery.
    """

    def __init__(self, max_size: int = 256) -> None:
        """Initialise an empty DLQ with the given ring-buffer cap.

        Args:
            max_size: maximum entries retained before the
                oldest are evicted. Default 256 — enough for
                a meaningful session-scoped sample without
                unbounded memory growth.
        """
        self._max_size = int(max_size)
        self._entries: list[dict[str, Any]] = []

    def record(self, event: str, payload: dict[str, Any] | None, reason: str) -> None:
        """Append one failed event entry; evict oldest if past cap.

        Stores the payload by reference — callers should treat
        their payloads as immutable after emission anyway. A
        ``None`` payload is normalised to an empty dict so
        downstream JSON serialisation never sees ``null``.

        Args:
            event: event identifier string (typically the
                ``Events.value``).
            payload: payload dict at emission time, or ``None``.
            reason: short tag describing why the event landed
                here (e.g. ``"all handlers raised"``,
                ``"timeout"``).
        """
        self._entries.append(
            {
                "event": event,
                "payload": payload or {},
                "reason": reason,
            }
        )
        if len(self._entries) > self._max_size:
            self._entries = self._entries[-self._max_size :]

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a shallow copy of the buffer in insertion order.

        Newest entries last (insertion order is preserved by
        the underlying list).

        Returns:
            List of entry dicts; safe to iterate while new
            entries are being recorded on another task (the
            copy is independent of the underlying buffer).
        """
        return list(self._entries)

    def clear(self) -> None:
        """Drop every recorded entry.

        Used at the start of a debug session to focus on
        events from that point forward, or after exporting
        the DLQ to disk so future failures don't re-include
        the already-saved ones.
        """
        self._entries.clear()

    def __len__(self) -> int:
        """Return the current number of buffered entries.

        Used by ``DebugSnapshot.collect`` to include a count
        in the output without copying the whole buffer.
        """
        return len(self._entries)
