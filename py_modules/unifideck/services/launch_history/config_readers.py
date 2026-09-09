"""Launch history config readers — typed parsers.

Three readers for the launch-history tunables :

* ``read_threshold`` — failure count that opens the circuit;
* ``read_window_seconds`` — rolling window over which failures
  accumulate;
* ``read_fast_boot_seconds`` — duration under which an exit is
  treated as a "quick exit" (likely a crash).
"""

from __future__ import annotations

from typing import Any

DEFAULT_THRESHOLD = 3
DEFAULT_WINDOW_SECONDS = 600.0
DEFAULT_FAST_BOOT_SECONDS = 10.0

def read_threshold(config: Any | None) -> int:
    """Read the failure threshold from the config.

    Args:
        config: optional config manager. When ``None`` returns
            the hard-coded default.

    Returns:
        Number of failures within the window that opens the
        circuit (default 3).
    """
    if config is None:
        return DEFAULT_THRESHOLD
    return int(
        config.get_int(
            "circuit_breaker.failures_threshold",
            DEFAULT_THRESHOLD,
        )
    )

def read_window_seconds(config: Any | None) -> float:
    """Read the rolling-window length from the config.

    Args:
        config: optional config manager.

    Returns:
        Window length in seconds. Default 600 (10 minutes) — short
        enough that a one-off crash days ago doesn't influence the
        decision, long enough to catch a repeated crash pattern.
    """
    if config is None:
        return DEFAULT_WINDOW_SECONDS
    return float(
        config.get_int(
            "circuit_breaker.window_seconds",
            int(DEFAULT_WINDOW_SECONDS),
        )
    )

def read_fast_boot_seconds(config: Any | None) -> float:
    """Read the fast-boot-failure cutoff from the config.

    Args:
        config: optional config manager.

    Returns:
        Cutoff in seconds. Default 10. A launch that exits with
        non-zero status in less time is treated as a likely crash;
        a launch that runs longer then crashes is treated as
        non-failure for circuit-breaker purposes.
    """
    if config is None:
        return DEFAULT_FAST_BOOT_SECONDS
    return float(
        config.get_int(
            "circuit_breaker.fast_boot_seconds",
            int(DEFAULT_FAST_BOOT_SECONDS),
        )
    )
