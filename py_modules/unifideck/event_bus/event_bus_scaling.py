"""Bus scaling — per-key event batching primitives.

Some emitters generate events at a frequency that would overwhelm
subscribers (download progress, frame-rate metrics, telemetry).
``BatchDispatcher`` accumulates events per key and signals when a
batch is ready to drain — either because the time window has
elapsed or because the batch reached its maximum size.

The class is intentionally **passive**: it doesn't run its own
timer. The caller (typically ``PriorityDispatcher``)
calls ``add`` for each event and gets back a "ready to drain"
boolean; when ready, ``drain`` returns the batched items and
resets the per-key window.

Public API:

* ``add(key, item)`` — append + return whether the batch should
  be flushed now;
* ``drain(key)``     — pop the buffered items and reset the
  per-key window timer;
* ``flush_all()``    — drain every buffer (typically called on
  bus shutdown or graceful flush);
* ``handler_supports_batch(handler)`` — predicate to detect
  handlers that opted in to receiving batched events via an
  ``on_batch`` method.

Typically used alongside coalescing in ``PriorityDispatcher`` —
coalescing merges repeated events of the same kind (e.g. only
the last progress wins), batching groups events into time-window
deliveries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

DEFAULT_BATCH_WINDOW_MS = 50
DEFAULT_BATCH_MAX_SIZE = 100

class BatchDispatcher:
    """Per-key event accumulator with size + time window triggers."""

    def __init__(
        self,
        *,
        window_ms: int = DEFAULT_BATCH_WINDOW_MS,
        max_size: int = DEFAULT_BATCH_MAX_SIZE,
    ) -> None:
        """Configure the batching windows.

        Two limits compose disjunctively — whichever fires first
        triggers the "ready to drain" signal:

        * **time window** — events older than ``window_ms`` ms
          since the last flush; default 50 ms (≈ one game-frame
          worth of buffering).
        * **size cap**    — at most ``max_size`` items per key;
          default 100, protects against unbounded memory growth
          when a key emits faster than the consumer drains.

        Args:
            window_ms: time window in milliseconds.
            max_size: maximum items per key before forcing a
                drain regardless of time.
        """
        self._window_ms = window_ms
        self._max_size = max_size
        self._buffers: dict[str, list[Any]] = {}
        self._last_flush_ms: dict[str, float] = {}

    def add(self, key: str, item: Any) -> bool:
        """Append ``item`` to the ``key`` buffer and report drain readiness.

        Initialises the per-key buffer if absent. Returns ``True``
        in either of two situations:

        1. Buffer reached ``max_size`` — size cap hit.
        2. Time since last flush (or since first add for a fresh
           key) is at or above ``window_ms``.

        Uses ``time.monotonic`` x 1000 for millisecond
        comparisons (immune to NTP wall-clock jumps).

        Args:
            key: discriminator (typically the coalesce key from
                ``event_priority.get_coalesce_key``).
            item: arbitrary payload to buffer.

        Returns:
            ``True`` if the caller should now call ``drain(key)``,
            ``False`` to keep accumulating.
        """
        buf = self._buffers.setdefault(key, [])
        buf.append(item)
        if len(buf) >= self._max_size:
            return True
        last = self._last_flush_ms.get(key)
        now_ms = time.monotonic() * 1000
        if last is None:
            self._last_flush_ms[key] = now_ms
            return False
        return (now_ms - last) >= self._window_ms

    def drain(self, key: str) -> list[Any]:
        """Pop and return the buffered items for ``key``, reset its window.

        ``pop`` semantics: the buffer is removed entirely (a
        subsequent ``add(key, ...)`` recreates it). The flush
        timestamp is updated to ``now`` so the next time-window
        check starts from the drain point.

        Args:
            key: same discriminator used in ``add``.

        Returns:
            Snapshot of the buffered items in insertion order.
            Empty list if no items had been buffered for ``key``.
        """
        items = self._buffers.pop(key, [])
        self._last_flush_ms[key] = time.monotonic() * 1000
        return items

    def flush_all(self) -> dict[str, list[Any]]:
        """Drain every non-empty buffer and clear all state.

        Used on bus shutdown / graceful flush to make sure no
        events are silently dropped. Note: this does **not**
        update ``_last_flush_ms`` (the buffer dict is wiped
        wholesale) — irrelevant since the dispatcher is
        terminating.

        Returns:
            Mapping ``key → [items]`` for every key with at
            least one buffered item.
        """
        out = {k: v for k, v in self._buffers.items() if v}
        self._buffers.clear()
        return out

    @staticmethod
    def handler_supports_batch(handler: Callable[..., Any]) -> bool:
        """Predicate: does ``handler`` opt in to batched delivery?

        A handler opts in by exposing two attributes:

        * ``supports_batch = True`` — the marker;
        * ``on_batch`` — the batched-delivery method.

        Decorators like ``@batchable`` typically set both. Used
        by the dispatcher to decide whether to call the regular
        handler (one event at a time) or its ``on_batch`` form
        (one call per drain).

        Args:
            handler: the callable to inspect.

        Returns:
            ``True`` iff both opt-in attributes are present and
            callable as expected.
        """
        return getattr(handler, "supports_batch", False) is True and callable(
            getattr(handler, "on_batch", None)
        )
