"""Brute-force detector — count recent failures, emit threshold events.

``BruteForceDetector`` tracks the total count of failed
authentication attempts (across **all** stores and users) within a
rolling time window. When the count crosses one of two thresholds,
a callback fires:

* **warning** — N failures in the window: emit a UI warning;
* **escalation** — M failures in the window (where M > N): emit a
  louder alert, intended to surface to the user via a non-
  dismissible toast (future work) and possibly disable auth flows
  temporarily.

The escalation is debounced — once raised, it stays raised until
``reset`` is called. The warning emits every time the threshold is
crossed (no debounce), which lets the UI keep a live counter.

State is in-memory only — a plugin restart wipes the counters.
This is intentional: a brute-force attack is a session-scoped
concern, not a persistent one.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

class BruteForceDetector:
    """Rolling-window failure counter with two-tier alerting."""

    def __init__(
        self,
        window_seconds: float,
        warning_threshold: int,
        escalation_threshold: int,
        on_threshold_crossed: Callable[..., None],
    ) -> None:
        """Configure the detector with thresholds + alert callback.

        Buffer size is ``2 * escalation_threshold`` so the deque
        never grows unbounded but always has enough slack to count
        the escalation threshold within the window even if
        failures arrive faster than they age out.

        Args:
            window_seconds: rolling-window length.
            warning_threshold: failure count that triggers the
                warning-level callback.
            escalation_threshold: failure count that triggers the
                escalation-level callback (must be > warning).
            on_threshold_crossed: callable invoked with
                ``level=<warning|escalation>`` and
                ``recent_failures=<int>`` when a threshold is
                crossed. Typically the ``SecurityService``'s
                ``_emit_bruteforce`` method.
        """
        self._window = window_seconds
        self._warning = warning_threshold
        self._escalation = escalation_threshold
        self._failures: deque[float] = deque(maxlen=escalation_threshold * 2)
        self._escalated = False
        self._on_crossed = on_threshold_crossed

    def check(self) -> None:
        """Record a new failure and evaluate the thresholds.

        Called by the auth-audit mixin every time a
        ``STORE_AUTH_FAILED`` event arrives. Two-step evaluation:

        1. Append the current timestamp to the rolling buffer.
        2. Count the timestamps within the window.

        Threshold decisions:

        * **At or above escalation, not yet escalated** → mark
          escalated, log at ERROR, fire callback with
          ``"escalation"``.
        * **At or above warning** (but below escalation, or
          already escalated) → log at WARN, fire callback with
          ``"warning"``.
        * **Below warning** → no action.

        Uses ``time.monotonic`` (not ``time.time``) so wall-clock
        adjustments (NTP sync, user changing the system time) can't
        confuse the rolling-window comparison.
        """
        now = time.monotonic()
        self._failures.append(now)
        recent = sum(1 for ts in self._failures if now - ts <= self._window)
        if recent >= self._escalation and not self._escalated:
            self._escalated = True
            logger.error(
                "[BruteForceDetector] ESCALATION: %d failures in %.0fs",
                recent,
                self._window,
            )
            self._on_crossed(level="escalation", recent_failures=recent)
        elif recent >= self._warning:
            logger.warning(
                "[BruteForceDetector] warning: %d failures in %.0fs",
                recent,
                self._window,
            )
            self._on_crossed(level="warning", recent_failures=recent)

    def status(self) -> dict[str, Any]:
        """Return a snapshot of the detector's current state.

        Used by the QAM UI to show the live counter and by tests
        to verify the configured thresholds.

        Returns:
            Dict with ``recent_failures`` (count within window),
            ``window_seconds``, ``warning_threshold``,
            ``escalation_threshold``, and ``escalated`` flag.
        """
        now = time.monotonic()
        recent = sum(1 for ts in self._failures if now - ts <= self._window)
        return {
            "recent_failures": recent,
            "window_seconds": self._window,
            "warning_threshold": self._warning,
            "escalation_threshold": self._escalation,
            "escalated": self._escalated,
        }

    def reset(self) -> None:
        """Clear the failure buffer and the escalation flag.

        Called by ``SecurityService.reset_bruteforce_state`` from
        the RPC layer (admin reset) or after a confirmed
        legitimate auth.
        """
        self._failures.clear()
        self._escalated = False
