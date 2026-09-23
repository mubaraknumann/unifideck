"""butlerd install notifications → the download queue's progress, plus a watchdog.

butler reports ``Progress {progress: 0..1, eta, bps}`` and ``Log {level,
message}`` notifications on the install's connection. This module forwards the
former to the ``DownloadService`` callback in the shape every store uses
(``phase``/``percentage``/``speed_bps``/``eta_seconds``), and fails an install
only when the connection has gone quiet.

"Quiet" is deliberately not "no bytes for a while". A slow CDN path was
measured delivering ~20 KB/s for 15 minutes on a live socket, with a 167 s gap
between two extracted files. A 120 s silence rule (the CLI stores' default)
would have killed a healthy install. Any ``Progress`` advance or ``Log`` line
counts as life, and the window is configurable
(``stores.itch.stall_timeout_seconds``, default 900).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .butlerd import ButlerdError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Any], Awaitable[None]]

#: How often the watch loop forwards progress and checks for a stall.
_TICK_S = 1.0


class ProgressTracker:
    """Collects one install connection's notifications; see module docstring."""

    def __init__(self, progress_cb: ProgressCallback | None, *, stall_s: float) -> None:
        self._cb = progress_cb
        self._stall_s = stall_s
        self._phase = "preparing"
        self._last_activity = time.monotonic()
        self._fraction = -1.0
        self._pending: dict[str, Any] | None = None

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        """``ButlerdConnection`` notification hook (synchronous)."""
        if method == "Progress":
            fraction = float(params.get("progress") or 0.0)
            if fraction > self._fraction:
                self._fraction = fraction
                self._last_activity = time.monotonic()
            self._pending = {
                "phase": self._phase,
                "percentage": round(max(fraction, 0.0) * 100.0, 2),
                "speed_bps": float(params.get("bps") or 0.0),
                "eta_seconds": int(params.get("eta") or 0),
            }
        elif method == "Log":
            self._last_activity = time.monotonic()
        elif method in ("TaskStarted", "TaskSucceeded"):
            logger.info("[itch] butler %s (%s)", method, params.get("type"))

    async def phase(self, name: str) -> None:
        """Enter a new phase and tell the queue at once."""
        self._phase = name
        self._last_activity = time.monotonic()
        await self._emit({"phase": name})

    async def watch(self, call: Awaitable[dict[str, Any]]) -> dict[str, Any] | str:
        """Await *call*; its result, or an error message when it failed or stalled.

        Cancelling the caller cancels *call* and re-raises. Butler-side
        cleanup (``Install.Cancel``) is the caller's job, because it knows the id.
        """
        task = asyncio.ensure_future(call)
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=_TICK_S)
                await self._flush()
                quiet = time.monotonic() - self._last_activity
                if not task.done() and quiet > self._stall_s:
                    task.cancel()
                    return (f"stalled: no progress for {int(quiet)}s while "
                            f"{self._phase}")
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ButlerdError):
                await task
            raise
        await self._flush()
        try:
            return task.result()
        except ButlerdError as e:
            return f"itch_install_failed: {e}"

    async def _flush(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            await self._emit(pending)

    async def _emit(self, payload: dict[str, Any]) -> None:
        if self._cb is None:
            return
        try:
            await self._cb(payload)
        except Exception:
            logger.exception("[itch] progress callback failed")
