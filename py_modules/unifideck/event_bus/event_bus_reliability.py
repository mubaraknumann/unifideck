"""Bus reliability — per-handler circuit breaker with rolling failure rate.

When a subscriber handler keeps raising exceptions, the bus protects
itself (and the rest of the subscribers) by opening a circuit breaker
on that handler — it stops being invoked until a cooldown elapses.

Two types in this module:

* ``_CBState`` — per-handler internal state (rolling success/failure
  window + ``open_until`` timestamp);
* ``CircuitBreaker`` — the breaker itself, keyed by handler name.

Algorithm (simpler than the classic 3-state design):

1. **CLOSED** (default) — invocations allowed, every outcome is
   recorded into a rolling window of the last ``CB_WINDOW_SIZE`` (20)
   results.
2. When the window is full **and** the failure rate is at or above
   ``CB_OPEN_THRESHOLD`` (50%), the breaker opens for
   ``CB_RESET_TIMEOUT_SEC`` (30 s) — ``allow`` returns ``False``.
3. After the timeout, ``allow`` flips back to ``True`` automatically
   (the window is preserved, so a re-open is fast if the handler is
   still broken).

There's no explicit HALF_OPEN trial state — the design is "soft
retry": once the cooldown expires the next call is allowed
unconditionally, and a single new failure that pushes the rate back
above threshold re-opens immediately.

Failures themselves are written to the dead-letter queue in
``event_bus_extensions.py`` for post-mortem inspection.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CB_WINDOW_SIZE = 20
CB_OPEN_THRESHOLD = 0.5
CB_RESET_TIMEOUT_SEC = 30.0

@dataclass
class _CBState:
    """Per-handler circuit-breaker state.

    Attributes:
        window: rolling buffer of the last ``CB_WINDOW_SIZE``
            outcomes. ``True`` = success, ``False`` = failure.
            ``deque(maxlen=...)`` auto-evicts oldest entries.
        open_until: ``time.monotonic()`` value before which the
            breaker is open. ``0.0`` means closed.
    """

    window: deque[bool] = field(
        default_factory=lambda: deque(maxlen=CB_WINDOW_SIZE),
    )
    open_until: float = 0.0

class CircuitBreaker:
    """Per-handler circuit breaker driven by rolling failure rate."""

    def __init__(
        self,
        *,
        open_threshold: float = CB_OPEN_THRESHOLD,
        reset_timeout: float = CB_RESET_TIMEOUT_SEC,
    ) -> None:
        """Configure the breaker with its threshold + cooldown.

        Both knobs are constructor-only (no per-handler override)
        so every breaker in a given bus uses the same policy.

        Args:
            open_threshold: failure rate in [0, 1] that opens
                the breaker once the window is full. Default
                ``0.5`` (i.e. 50% — handler is more often broken
                than working).
            reset_timeout: cooldown in seconds before ``allow``
                returns ``True`` again. Default 30 s — long
                enough to let transient issues resolve, short
                enough to recover quickly when fixed.
        """
        self._open_threshold = open_threshold
        self._reset_timeout = reset_timeout
        self._state: dict[str, _CBState] = {}

    def allow(self, handler_name: str) -> bool:
        """Return whether ``handler_name`` may currently be invoked.

        Three cases:

        1. **No state yet** (handler never recorded an outcome) →
           allow (default behaviour for new handlers).
        2. **State exists, but ``open_until=0``** → closed, allow.
        3. **State exists, ``open_until > now``** → open, refuse.
        4. **State exists, ``open_until <= now``** → cooldown
           elapsed, reset ``open_until`` to 0 and allow.

        Args:
            handler_name: identifier of the handler being
                checked.

        Returns:
            ``True`` if the handler may be invoked, ``False`` if
            the circuit is currently open.
        """
        s = self._state.get(handler_name)
        if s is None or s.open_until == 0.0:
            return True
        if time.monotonic() >= s.open_until:
            s.open_until = 0.0
            return True
        return False

    def record(self, handler_name: str, success: bool) -> None:
        """Record a handler invocation outcome and maybe trip the breaker.

        Always appends to the rolling window. The threshold check
        is gated on a **full** window — partial windows are not
        evaluated (a single early failure wouldn't be statistically
        meaningful). Once the failure rate is at or above the
        threshold and the breaker isn't already open, it opens
        for ``_reset_timeout`` seconds and logs at WARN.

        Args:
            handler_name: identifier of the handler that just ran.
            success: outcome of the invocation — ``True`` for a
                clean run, ``False`` for an exception.
        """
        s = self._state.setdefault(handler_name, _CBState())
        s.window.append(success)
        if len(s.window) < (s.window.maxlen or 0):
            return
        failures = s.window.count(False)
        rate = failures / len(s.window)
        if rate >= self._open_threshold and s.open_until == 0.0:
            s.open_until = time.monotonic() + self._reset_timeout
            logger.warning(
                "[CircuitBreaker] %s opened (failure rate=%.0f%%)",
                handler_name,
                rate * 100,
            )

    def is_open(self, handler_name: str) -> bool:
        """Return whether the circuit is currently open for ``handler_name``.

        Unlike ``allow``, this method does **not** mutate state
        (no automatic close on cooldown elapse). Useful for
        introspection / diagnostics where the caller wants to
        peek without affecting the breaker.

        Args:
            handler_name: identifier of the handler.

        Returns:
            ``True`` if ``open_until`` is in the future, ``False``
            otherwise (closed or no state recorded).
        """
        s = self._state.get(handler_name)
        return s is not None and s.open_until > time.monotonic()
