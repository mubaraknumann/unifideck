"""services/launch_history/service.py — Per-game launch failure tracking.

Circuit breaker for game launches: N failures within a sliding
window → refuse subsequent launches until window expires or user
resets. Distinct from ``PlaytimeService`` (permanent session
tracking) — failures are ephemeral and window-bounded.
Storage: ``~/.local/share/unifideck/launch_history.json``, atomic
writes. Filesystem-as-IPC between the out-of-process launcher
(writer) and the plugin (reader).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe

from .bypass import _BypassMixin
from .config_readers import (
    DEFAULT_FAST_BOOT_SECONDS,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW_SECONDS,
    read_fast_boot_seconds,
    read_threshold,
    read_window_seconds,
)
from .constants import FAILURE_KIND_FAST_BOOT
from .failures import _FailuresMixin

logger = logging.getLogger(__name__)

# Strong references to background event-emit tasks so the GC can't
# collect them mid-flight (see RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


class LaunchHistoryService(_FailuresMixin, _BypassMixin):
    """Tracks per-game launch failures within a sliding window."""

    # Backwards-compat class attrs — source of truth in config_readers.py.
    DEFAULT_THRESHOLD = DEFAULT_THRESHOLD
    DEFAULT_WINDOW_SECONDS = DEFAULT_WINDOW_SECONDS
    DEFAULT_FAST_BOOT_SECONDS = DEFAULT_FAST_BOOT_SECONDS

    def __init__(
        self,
        config: Any | None = None,
        storage_path: Path | None = None,
        bus: Any | None = None,
    ) -> None:
        """Store refs; no I/O at construction."""
        self._config = config

        if storage_path is None:
            self._path = Path("~/.local/share/unifideck/launch_history.json").expanduser()
        else:
            self._path = storage_path

        self._bus = bus

        # Monotonic start time per ``game_key``, set by
        # ``_on_game_launched``. Needed because no ``GAME_STOPPED``
        # emitter carries an elapsed time, and the fast-boot rule
        # (exited non-zero *quickly*) cannot be evaluated without one.
        # ``time.monotonic`` rather than wall clock so a suspend
        # mid-game does not read as a long, healthy session — the same
        # reasoning as ``services/playtime/service.py``.
        self._started_at: dict[str, float] = {}

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` guarded by
        # ``hasattr`` — but ``auto_wire`` is module-level,
        # not a bus method, so the hasattr check returned
        # False and every subscription was silently dropped.
        # The ``self._bus is not None`` guard is preserved
        # because the constructor accepts ``bus=None`` for
        # test harnesses.
        if self._bus is not None:
            auto_wire(self, self._bus)

    def threshold(self) -> int:
        """Live read of ``circuit_breaker.failures_threshold``."""
        return read_threshold(self._config)

    def window_seconds(self) -> float:
        """Live read of ``circuit_breaker.window_seconds``."""
        return read_window_seconds(self._config)

    def fast_boot_seconds(self) -> float:
        """Live read of ``circuit_breaker.fast_boot_seconds``."""
        return read_fast_boot_seconds(self._config)

    def _emit_state(self, game_key: str, trigger: str) -> None:
        """Fire-and-forget ``CIRCUIT_STATE_CHANGED`` on the bus."""
        if not self._bus:
            return
        # Capture a local reference so mypy narrows it across
        # the inner ``_emit`` closure. Without this binding, the
        # closure sees ``self._bus: Any | None`` (mypy doesn't
        # propagate the outer narrowing into the nested function)
        # and ``bus.emit`` raises ``Item "None" ... has no attribute``.
        bus = self._bus

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop

        async def _emit() -> None:
            try:
                is_open, count = self.is_circuit_open(game_key)
                store, game_id = game_key.split(":", 1)

                await bus.emit(
                    Events.CIRCUIT_STATE_CHANGED,
                    store=store,
                    game_id=game_id,
                    is_open=is_open,
                    failure_count=count,
                    trigger=trigger,
                )
            except Exception as e:
                logger.warning("[LaunchHistory] Failed to emit circuit state: %s", e)

        _task = loop.create_task(_emit())
        _BACKGROUND_TASKS.add(_task)
        _task.add_done_callback(_BACKGROUND_TASKS.discard)

    @subscribe(Events.GAME_LAUNCHED)
    async def _on_game_launched(self, **kwargs: Any) -> None:
        """Stamp the launch so ``_on_game_stopped`` can measure it."""
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        if not store or not game_id:
            return
        self._started_at[f"{store}:{game_id}"] = time.monotonic()

    @subscribe(Events.GAME_STOPPED)
    async def _on_game_stopped(self, **kwargs: Any) -> None:
        """Classify a finished launch for the circuit breaker.

        Reads ``exit_code``/``elapsed_seconds`` — the names the
        emitters actually send and the ones ``CANONICAL_SCHEMA``
        declares. It previously read ``rc``/``elapsed``, which no
        emitter has ever sent, so ``rc`` was always ``None``, this
        handler returned at the guard below on every single stop, and
        :meth:`record_success` had no reachable call site anywhere in
        the tree — the breaker could accumulate failures but never
        clear on a good launch. Audit correction C-2.

        Note only the plugin-side emitter (``notify_game_stopped``)
        carries an exit code; the launcher subprocess emits
        ``store``/``game_id`` alone, so on that bus this still returns
        at the ``exit_code is None`` guard, as it always did.
        """
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        rc = kwargs.get("exit_code")

        if not store or not game_id:
            return

        game_key = f"{store}:{game_id}"
        started = self._started_at.pop(game_key, None)
        elapsed = kwargs.get("elapsed_seconds")
        if elapsed is None:
            elapsed = time.monotonic() - started if started is not None else 0.0

        # Determine success or failure
        if rc == 0:
            self.record_success(game_key)
            return

        if rc is None:
            return

        # Ignore if terminated by signal (user cancel)
        # Shell convention: > 128 is signal
        if rc > 128:
            logger.debug("[LaunchHistory] Ignoring signal termination %d for %s", rc, game_key)
            return

        # Non-zero exit code
        if elapsed < self.fast_boot_seconds():
            self.record_failure(game_key, FAILURE_KIND_FAST_BOOT, f"rc={rc}")
        else:
            logger.debug(
                "[LaunchHistory] Ignoring non-zero rc=%d for %s (ran for %.1fs >= fast_boot %.1fs)",
                rc, game_key, elapsed, self.fast_boot_seconds()
            )
