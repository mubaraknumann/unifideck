"""Bus handler watchdog — timeout, quarantine, recovery.

``HandlerWatchdog`` enforces a per-handler timeout: every bus
emission goes through ``invoke`` which wraps the handler call in
``asyncio.wait_for``. On timeout, the handler is cancelled and
the timeout counter is bumped.

After ``quarantine_threshold`` **consecutive** timeouts (default
10), the handler is quarantined — subsequent ``invoke`` calls
raise ``HandlerQuarantinedError`` immediately (no attempt to
invoke the handler at all). The bus catches this exception and
skips the handler for the rest of the emission.

Recovery happens two ways:

* ``release_quarantine(name)`` — manual release (typically from
  the dev UI's "release handler" button);
* a single successful ``invoke`` resets ``consecutive_timeouts``
  to 0 — once released, a working handler stays clean.

There's also a **pre-emptive** quarantine via
``quarantine_preemptive(name, reason)`` — used by
``ProbeReactionService`` to disable handlers known to depend on
a currently-broken capability (e.g. SteamClient JS bridge down)
before they get a chance to time out.

Public types:

* ``HandlerTimeoutMetrics`` — mutable record kept per handler;
* ``HandlerWatchdog`` — the main class;
* ``HandlerQuarantinedError`` — raised by ``invoke`` when the
  handler is currently quarantined.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HANDLER_TIMEOUT_SEC = 5.0
DEFAULT_QUARANTINE_THRESHOLD = 10

@dataclass
class HandlerTimeoutMetrics:
    """Per-handler watchdog state and counters.

    Attributes:
        name: handler identifier.
        invocations: total successful + failed invocation count.
        timeouts: lifetime timeout count (never reset).
        consecutive_timeouts: timeouts since the last successful
            call — resets to 0 on any success.
        quarantined: ``True`` while the handler is suspended.
        last_error: short human-readable description of the
            most recent error (timeout or pre-emptive
            quarantine reason). ``None`` after a clean call.
    """

    name: str
    invocations: int = 0
    timeouts: int = 0
    consecutive_timeouts: int = 0
    quarantined: bool = False
    last_error: str | None = None

class HandlerWatchdog:
    """Per-handler timeout supervisor with quarantine on repeated failure."""

    def __init__(
        self,
        *,
        default_timeout: float = DEFAULT_HANDLER_TIMEOUT_SEC,
        quarantine_threshold: int = DEFAULT_QUARANTINE_THRESHOLD,
    ) -> None:
        """Configure the watchdog with default budget + threshold.

        Per-handler overrides for the timeout are possible via
        ``register(name, timeout=...)``. The threshold is global.

        Args:
            default_timeout: seconds budget per invocation
                (default 5.0). Generous enough for typical bus
                handlers, tight enough to catch genuine hangs.
            quarantine_threshold: consecutive timeouts before
                the handler is quarantined (default 10). High
                enough that transient slowness doesn't trigger
                a false positive.
        """
        self._default_timeout = default_timeout
        self._quarantine_threshold = quarantine_threshold
        self._metrics: dict[str, HandlerTimeoutMetrics] = {}
        self._timeouts: dict[str, float] = {}

    def register(self, handler_name: str, timeout: float | None = None) -> None:
        """Register a handler, optionally with a custom timeout.

        Idempotent: re-registering an existing handler updates
        the timeout (if provided) but doesn't reset its metrics.
        Used by the bus to declare a handler upfront so a
        timeout entry exists before the first invocation.

        Args:
            handler_name: handler identifier.
            timeout: optional per-handler override. ``None``
                means "use ``default_timeout``".
        """
        if timeout is not None:
            self._timeouts[handler_name] = timeout
        if handler_name not in self._metrics:
            self._metrics[handler_name] = HandlerTimeoutMetrics(
                name=handler_name,
            )

    def unregister(self, handler_name: str) -> None:
        """Drop a handler's custom timeout and clear its quarantine.

        Called by the bus when a handler is unsubscribed. The
        ``HandlerTimeoutMetrics`` record is kept (preserves
        lifetime invocation count for diagnostics) but the
        quarantine state is cleared so a future re-registration
        starts cleanly.

        Args:
            handler_name: handler identifier.
        """
        self._timeouts.pop(handler_name, None)
        m = self._metrics.get(handler_name)
        if m is not None:
            m.quarantined = False
            m.consecutive_timeouts = 0

    async def invoke(
        self,
        handler_name: str,
        handler: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Invoke ``handler`` with timeout enforcement and metrics tracking.

        Workflow:

        1. Lazy-create the metrics record on first call.
        2. **If quarantined** → raise ``HandlerQuarantinedError``
           without invoking. The bus catches this and skips.
        3. Resolve the timeout (per-handler override, or
           default).
        4. Run via ``asyncio.wait_for`` to enforce the budget.
        5. **On success** → reset ``consecutive_timeouts``,
           clear ``last_error``, return the result.
        6. **On timeout** → record via ``_record_timeout``
           (which may quarantine), re-raise ``TimeoutError``.

        Args:
            handler_name: handler identifier.
            handler: the coroutine function to call.
            *args / **kwargs: forwarded to the handler.

        Returns:
            Whatever ``handler`` returned.

        Raises:
            HandlerQuarantinedError: if the handler is currently
                quarantined.
            TimeoutError: if the handler exceeded its budget.
        """
        metrics = self._metrics.setdefault(
            handler_name,
            HandlerTimeoutMetrics(name=handler_name),
        )
        if metrics.quarantined:
            raise HandlerQuarantinedError(handler_name)
        timeout = self._timeouts.get(handler_name, self._default_timeout)
        metrics.invocations += 1
        try:
            result = await asyncio.wait_for(
                handler(*args, **kwargs),
                timeout=timeout,
            )
            metrics.consecutive_timeouts = 0
            metrics.last_error = None
            return result
        except TimeoutError:
            self._record_timeout(metrics, timeout)
            raise

    def release_quarantine(self, handler_name: str) -> bool:
        """Manually clear a handler's quarantine state.

        Used by the dev UI's "release handler" button and by
        recovery flows that know a previously-broken handler
        is now repairable. Resets ``consecutive_timeouts`` so
        a single timeout right after release won't immediately
        re-quarantine.

        Args:
            handler_name: handler identifier.

        Returns:
            ``True`` if the handler was quarantined and is now
            released, ``False`` if it wasn't quarantined or
            doesn't exist.
        """
        m = self._metrics.get(handler_name)
        if m is None or not m.quarantined:
            return False
        m.quarantined = False
        m.consecutive_timeouts = 0
        logger.info(
            "[HandlerWatchdog] released %s from quarantine",
            handler_name,
        )
        return True

    def quarantine_preemptive(
        self,
        handler_name: str,
        reason: str = "preemptive",
    ) -> bool:
        """Quarantine a handler **before** it gets a chance to time out.

        Used by ``ProbeReactionService`` when a runtime
        probe reports that a capability the handler depends on
        is currently broken — quarantining pre-emptively avoids
        wasted timeouts that would only end up at the same
        result.

        Idempotent on already-quarantined handlers (returns
        ``False`` to signal "no change made").

        Args:
            handler_name: handler identifier.
            reason: short tag (e.g. ``"probe:steam_client_apps"``)
                stored in ``last_error`` for diagnostics.

        Returns:
            ``True`` if the quarantine was newly applied,
            ``False`` if the handler was already quarantined.
        """
        metrics = self._metrics.setdefault(
            handler_name,
            HandlerTimeoutMetrics(name=handler_name),
        )
        if metrics.quarantined:
            return False
        metrics.quarantined = True
        metrics.last_error = f"quarantined preemptively: {reason}"
        logger.warning(
            "[HandlerWatchdog] %s quarantined preemptively (%s)",
            handler_name,
            reason,
        )
        return True

    def get_metrics(self) -> dict[str, HandlerTimeoutMetrics]:
        """Return a shallow copy of every handler's metrics.

        Shallow on purpose: the ``HandlerTimeoutMetrics``
        records are shared by reference. Callers should treat
        them as read-only.

        Returns:
            Mapping ``handler_name → HandlerTimeoutMetrics``.
        """
        return dict(self._metrics)

    def _record_timeout(self, metrics: HandlerTimeoutMetrics, timeout: float) -> None:
        """Bump the timeout counters and possibly trigger quarantine.

        Side effects:

        * ``timeouts`` and ``consecutive_timeouts`` both
          increment.
        * ``last_error`` is set to a human-readable summary.
        * Logged at WARN with the consecutive count vs threshold.
        * If ``consecutive_timeouts`` reaches the threshold,
          flip ``quarantined`` to ``True`` and log at ERROR.

        Args:
            metrics: the handler's metrics record (mutated).
            timeout: the budget that was exceeded — used to
                build a useful ``last_error`` string.
        """
        metrics.timeouts += 1
        metrics.consecutive_timeouts += 1
        metrics.last_error = f"timeout after {timeout:.1f}s"
        logger.warning(
            "[HandlerWatchdog] %s timed out (%d/%d consecutive)",
            metrics.name,
            metrics.consecutive_timeouts,
            self._quarantine_threshold,
        )
        if metrics.consecutive_timeouts >= self._quarantine_threshold:
            metrics.quarantined = True
            logger.error(
                "[HandlerWatchdog] QUARANTINED %s after %d "
                "consecutive timeouts — will be skipped until "
                "release_quarantine() is called",
                metrics.name,
                metrics.consecutive_timeouts,
            )

class HandlerQuarantinedError(Exception):
    """Raised by ``HandlerWatchdog.invoke`` when the handler is suspended."""

    def __init__(self, handler_name: str) -> None:
        """Build the exception with the offending handler name.

        Stores the handler name as an attribute so the bus
        (which catches this) can include it in the supervision
        event without re-parsing the message.

        Args:
            handler_name: handler identifier that triggered
                the exception.
        """
        super().__init__(f"handler {handler_name} is quarantined")
        self.handler_name = handler_name
