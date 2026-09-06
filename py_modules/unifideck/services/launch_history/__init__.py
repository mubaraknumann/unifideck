"""Launch history service — record per-launch outcomes.

Re-exports ``LaunchHistoryService``. The service maintains a per-game
history of recent launches with their outcome (success, crash,
quick-exit) so the circuit breaker, telemetry, and UI can decide
when something is wrong.
"""

from __future__ import annotations

from .constants import FAILURE_KIND_FAST_BOOT, FAILURE_KIND_LAUNCHER_ERROR
from .service import LaunchHistoryService

__all__ = [
    "FAILURE_KIND_FAST_BOOT",
    "FAILURE_KIND_LAUNCHER_ERROR",
    "LaunchHistoryService",
]
