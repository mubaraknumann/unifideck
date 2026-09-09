"""Bus supervision sub-package — public exports.

Re-exports the two supervision components that wrap the bus at
runtime : ``HandlerLatencyCollector`` (per-handler latency
histograms) and ``HandlerWatchdog`` (per-handler timeout quarantine).

These wrap every subscription registered on the bus so that
operational health is observable (latency distribution) and
self-healing (quarantine slow/hung handlers).
"""

from __future__ import annotations

from .metrics_handler import HandlerLatencyCollector
from .watchdog_handler import HandlerWatchdog

__all__ = ["HandlerLatencyCollector", "HandlerWatchdog"]
