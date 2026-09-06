"""Event replay buffer — per-event-type ring buffers of recent events.

``EventReplayBuffer`` is **not** a single FIFO of all events — it's
a dict of per-event-type ``deque(maxlen=...)`` buffers. Different
event types get different caps:

* high-frequency events (``SYNC_PROGRESS``, ``DOWNLOAD_PROGRESS``)
  → cap 50 (recent progress only);
* lifecycle events (``GAME_UNINSTALLED``, ``STORE_AUTH_COMPLETE``)
  → cap 10-20 (full history of recent state changes);
* anything else → fallback cap (20).

Two primary use cases:

* **Late subscribers** — a service that subscribes after the bus
  started can snapshot the buffer to backfill its state (e.g.
  ``SecurityService.start`` drains
  ``CONFIG_VALIDATION_FAILED`` events that fired during boot).
* **Debugging** — the QAM debug panel uses ``snapshot()`` to show
  what happened just before an error.

Public API:

* ``record(event, kwargs)``   — push into the per-type ring;
* ``snapshot(events, limit)`` — flatten + sort by timestamp,
  optionally filtered to a subset of event types;
* ``size(event)``             — observability (total or per-type
  count);
* ``clear()``                 — wipe everything (test-only).

State is in-memory; restart wipes it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from unifideck.core.types import Events

MAX_SNAPSHOT_ENTRIES = 500
_DEFAULT_CAPS: dict[Events, int] = {
    Events.SYNC_PROGRESS: 50,
    Events.DOWNLOAD_PROGRESS: 50,
    Events.GAME_UNINSTALLED: 20,
    Events.STORE_AUTH_COMPLETE: 10,
    Events.STORE_LOGOUT: 10,
}
_FALLBACK_CAP = 20

@dataclass
class _RecordedEvent:
    """One recorded event entry.

    Attributes:
        event: event name as a plain string (the ``Events``
            enum's ``.value``). String storage so the snapshot
            output is directly JSON-serialisable for RPC.
        kwargs: payload dict from the original emission. Stored
            by reference — caller is expected not to mutate it
            after emission (the bus convention).
        timestamp: ``time.monotonic()`` at record time.
    """

    event: str
    kwargs: dict[str, Any]
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation for snapshots.

        Rounds the timestamp to 3 decimals (millisecond
        precision) — sub-millisecond detail is noise for
        diagnostic display.

        Returns:
            Dict with ``event`` (str), ``kwargs`` (dict),
            ``timestamp`` (float, rounded).
        """
        return {
            "event": self.event,
            "kwargs": self.kwargs,
            "timestamp": round(self.timestamp, 3),
        }

class EventReplayBuffer:
    """Per-event-type ring buffers with custom caps."""

    def __init__(
        self,
        *,
        fallback_cap: int = _FALLBACK_CAP,
        caps: dict[Events, int] | None = None,
    ) -> None:
        """Initialise with default + optional override caps.

        Defaults from ``_DEFAULT_CAPS`` are copied (not mutated),
        then any ``caps`` overrides are merged on top. The
        ``fallback_cap`` is used for event types absent from
        both tables.

        Args:
            fallback_cap: cap for event types without an
                explicit entry (default 20).
            caps: optional per-event override. Useful for
                testing (smaller buffers) or for special-case
                deployments.
        """
        self._fallback_cap = fallback_cap
        self._caps = dict(_DEFAULT_CAPS)
        if caps:
            self._caps.update(caps)
        self._buffers: dict[str, deque[_RecordedEvent]] = {}

    def record(self, event: Events | str, kwargs: dict[str, Any]) -> None:
        """Push a new ``_RecordedEvent`` into the per-type buffer.

        Lazy buffer creation: the first time an event type is
        recorded, its deque is built with the resolved cap.
        Subsequent ``record`` calls for the same type reuse it.
        ``deque(maxlen=...)`` auto-evicts the oldest entry when
        the cap is reached.

        Stores ``event`` as a string (the enum's ``.value``) so
        snapshot output is JSON-friendly.

        Args:
            event: ``Events`` enum value or its string form.
            kwargs: payload dict from the bus emission.
        """
        event_str = event.value if isinstance(event, Events) else str(event)
        buf = self._buffers.get(event_str)
        if buf is None:
            cap = self._resolve_cap(event)
            buf = deque(maxlen=cap)
            self._buffers[event_str] = buf
        buf.append(
            _RecordedEvent(
                event=event_str,
                kwargs=kwargs,
                timestamp=time.monotonic(),
            ),
        )

    def snapshot(
        self,
        events: Iterable[Events | str] | None = None,
        limit: int = MAX_SNAPSHOT_ENTRIES,
    ) -> list[dict[str, Any]]:
        """Return a flattened, timestamp-sorted view of recent events.

        Iterates every per-type buffer, optionally filtered to
        only the requested types, flattens them into a single
        list, sorts newest-first by timestamp, and truncates to
        ``limit`` (capped by ``MAX_SNAPSHOT_ENTRIES`` to bound
        the RPC payload size).

        Each entry goes through ``_RecordedEvent.to_dict`` so
        the result is directly JSON-serialisable.

        Args:
            events: optional iterable of event types to include.
                ``None`` (default) returns every type.
            limit: maximum entries returned (hard-capped at
                ``MAX_SNAPSHOT_ENTRIES`` = 500).

        Returns:
            List of entry dicts, newest first.
        """
        limit = min(limit, MAX_SNAPSHOT_ENTRIES)
        wanted = self._resolve_wanted_set(events)
        gathered: list[_RecordedEvent] = []
        for event_str, buf in self._buffers.items():
            if wanted is not None and event_str not in wanted:
                continue
            gathered.extend(buf)
        gathered.sort(key=lambda r: r.timestamp, reverse=True)
        return [r.to_dict() for r in gathered[:limit]]

    def size(self, event: Events | str | None = None) -> int:
        """Return the number of buffered entries, total or per event type.

        Args:
            event: optional event type. ``None`` (default) sums
                across every type; otherwise returns the count
                for that specific type only (0 if the type has
                no buffer yet).

        Returns:
            Entry count.
        """
        if event is None:
            return sum(len(b) for b in self._buffers.values())
        event_str = event.value if isinstance(event, Events) else str(event)
        buf = self._buffers.get(event_str)
        return len(buf) if buf is not None else 0

    def clear(self) -> None:
        """Drop every buffered event across all types.

        Test-only — there's no production scenario where wiping
        the replay buffer is desirable. Wipes the entire dict
        so subsequent ``record`` calls recreate buffers on
        demand.
        """
        self._buffers.clear()

    def _resolve_cap(self, event: Events | str) -> int:
        """Look up the cap for an event type with fallback.

        Strings are converted to ``Events`` first (silently
        falling back if not a known enum value). The fallback
        ``_fallback_cap`` ensures every event type gets at
        least some buffering.

        Args:
            event: ``Events`` or string form.

        Returns:
            The configured cap (default 20).
        """
        if isinstance(event, Events):
            return self._caps.get(event, self._fallback_cap)
        try:
            resolved = Events(event)
            return self._caps.get(resolved, self._fallback_cap)
        except ValueError:
            return self._fallback_cap

    @staticmethod
    def _resolve_wanted_set(events: Iterable[Events | str] | None) -> set[Any] | None:
        """Normalise the optional filter iterable into a set of strings.

        Used by ``snapshot`` to convert the per-call filter into
        the same string form used as buffer keys, so the
        membership test is fast.

        Args:
            events: optional iterable from the caller.

        Returns:
            Set of string event names, or ``None`` to mean
            "no filter".
        """
        if events is None:
            return None
        out: set[Any] = set()
        for e in events:
            out.add(e.value if isinstance(e, Events) else str(e))
        return out
