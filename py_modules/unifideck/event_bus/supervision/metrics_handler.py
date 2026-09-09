"""Per-handler latency metrics — track invocation timing per subscriber.

Two cooperating types:

* ``HandlerLatencyStats`` — one record per handler, accumulating
  ``invocations``, ``total_ms``, ``max_ms`` and computing live
  ``p50_ms`` / ``p95_ms`` over a rolling window of the last 100
  measurements.
* ``HandlerLatencyCollector`` — the top-level dict-of-stats keyed
  by handler name, with helpers to dump a snapshot or the
  top-N slowest handlers.

The percentile computation uses ``statistics.quantiles(n=20)`` —
divides the sorted window into 20 buckets, so element 9 is the
median (p50) and element 18 is approximately the 95th percentile.
Cheap enough to recompute on every ``record`` call without
profiling concerns.

Used by:

* the dev UI ("which handler is slow?" panel);
* the watchdog to decide which handler to quarantine
  when bus throughput drops.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

ROLLING_WINDOW_SIZE = 100

@dataclass
class HandlerLatencyStats:
    """Per-handler timing statistics + rolling window.

    Attributes:
        name: handler identifier (typically
            ``module.Class.method``).
        invocations: total count, monotonically increasing.
        total_ms: cumulative duration, used for the global
            average (not just the rolling window).
        max_ms: all-time maximum duration observed.
        p50_ms / p95_ms: percentiles over the rolling window —
            recomputed on every ``record``.
        _window: bounded buffer of the last
            ``ROLLING_WINDOW_SIZE`` (100) durations.
    """

    name: str
    invocations: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    _window: deque[float] = field(
        default_factory=lambda: deque(maxlen=ROLLING_WINDOW_SIZE),
    )

    def record(self, duration_ms: float) -> None:
        """Add a duration measurement and update all aggregates.

        Updates four state pieces atomically:

        1. Bump ``invocations``;
        2. Add to ``total_ms`` for the cumulative average;
        3. Update ``max_ms`` if this measurement is the new max
           (running maximum, never decays — by design, max is
           a "worst ever seen" alert signal);
        4. Push into the rolling window and recompute the live
           p50/p95.

        Args:
            duration_ms: handler execution time in milliseconds.
        """
        self.invocations += 1
        self.total_ms += duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        self._window.append(duration_ms)
        self._recompute_percentiles()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict with ms values rounded.

        Computes the cumulative average inline (not stored on
        the dataclass — derived from ``total_ms / invocations``).
        Every duration is rounded to 2 decimals (0.01 ms
        precision) — sub-10 µs detail is meaningless given the
        measurement overhead.

        Returns:
            Dict with ``name``, ``invocations``, ``total_ms``,
            ``avg_ms``, ``max_ms``, ``p50_ms``, ``p95_ms``.
        """
        avg = self.total_ms / self.invocations if self.invocations else 0.0
        return {
            "name": self.name,
            "invocations": self.invocations,
            "total_ms": round(self.total_ms, 2),
            "avg_ms": round(avg, 2),
            "max_ms": round(self.max_ms, 2),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
        }

    def _recompute_percentiles(self) -> None:
        """Refresh ``p50_ms`` / ``p95_ms`` from the rolling window.

        Edge cases:

        * **Empty window** (only happens just after reset) →
          no-op.
        * **Single sample** → both p50 and p95 equal that
          sample (no real distribution yet).
        * **2+ samples** → ``statistics.quantiles(n=20)``
          divides the sorted window into 20 buckets;
          ``qs[9]`` is the median (p50), ``qs[18]`` is
          approximately the 95th percentile.

        Called on every ``record`` — cheap enough at window
        size 100 (≈ µs).
        """
        n = len(self._window)
        if n == 0:
            return
        if n == 1:
            self.p50_ms = self.p95_ms = self._window[0]
            return
        qs = statistics.quantiles(self._window, n=20)
        self.p50_ms = qs[9]
        self.p95_ms = qs[18]

class HandlerLatencyCollector:
    """Top-level registry of per-handler latency stats."""

    def __init__(self) -> None:
        """Initialise with an empty stats dict.

        Handlers are added lazily on first ``record`` — no need
        to pre-declare them, which would require knowledge of
        the full subscriber list at bus construction time.
        """
        self._stats: dict[str, HandlerLatencyStats] = {}

    def record(self, handler_name: str, duration_ms: float) -> None:
        """Record a measurement for ``handler_name``.

        Lazy-creates the ``HandlerLatencyStats`` record on first
        call. Delegates to the record's own ``record`` method
        for the aggregation logic.

        Args:
            handler_name: handler identifier.
            duration_ms: measured duration in milliseconds.
        """
        stats = self._stats.get(handler_name)
        if stats is None:
            stats = HandlerLatencyStats(name=handler_name)
            self._stats[handler_name] = stats
        stats.record(duration_ms)

    def get_snapshot(self) -> dict[str, dict[str, float]]:
        """Return per-handler stats as a JSON-friendly dict-of-dicts.

        Used by the dev UI to render the full latency table.

        Returns:
            Mapping ``handler_name → stats_dict``. Each value
            is the output of ``HandlerLatencyStats.to_dict``.
        """
        return {name: stats.to_dict() for name, stats in self._stats.items()}

    def get_top_n(self, n: int = 10) -> dict[str, dict[str, float]]:
        """Return the ``n`` slowest handlers ranked by p95.

        Why p95 (not max or mean): p95 captures sustained
        slowness, immune to one-off spikes (max) and dilution
        by fast calls (mean). Used by the dev UI to highlight
        problem handlers.

        Args:
            n: maximum number of entries to return (default 10).

        Returns:
            Same shape as ``get_snapshot`` but truncated and
            ordered by p95 descending.
        """
        sorted_stats = sorted(
            self._stats.values(),
            key=lambda s: s.p95_ms,
            reverse=True,
        )
        return {s.name: s.to_dict() for s in sorted_stats[:n]}

    def reset(self, handler_name: str) -> bool:
        """Replace the stats record for ``handler_name`` with a fresh one.

        Used after a deployment / config change when historical
        data would be misleading. The handler keeps its identity
        but starts counting from zero.

        Args:
            handler_name: handler identifier.

        Returns:
            ``True`` if a record existed and was reset, ``False``
            if no such handler had been recorded yet (no-op).
        """
        if handler_name in self._stats:
            self._stats[handler_name] = HandlerLatencyStats(
                name=handler_name,
            )
            return True
        return False
