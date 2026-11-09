"""stores/microsoft/xodus/throttle.py — coalesce frequent status events."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class Throttle:
    """Runs ``emit`` at most once per interval, always with the latest state."""

    def __init__(self, emit: Callable[[], Coroutine[Any, Any, None]], interval: float) -> None:
        self._emit = emit
        self._interval = interval
        self._last = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def poke(self) -> None:
        if self._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, self._last + self._interval - loop.time())
        self._timer = loop.call_later(delay, self._fire)

    def _fire(self) -> None:
        self._timer = None
        self._last = asyncio.get_running_loop().time()
        task = asyncio.create_task(self._emit())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
