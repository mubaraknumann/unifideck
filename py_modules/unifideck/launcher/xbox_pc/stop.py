"""launcher/xbox_pc/stop.py — stop a running Xbox PC game without losing its save.

SIGINT kills a GUI game at once (Balatro exits 58), which skips its own
save-on-quit and the runtime's last cloud upload (I-018). So a Stop runs in
stages:

1. ``taskkill /IM <exe>`` without ``/F`` posts WM_CLOSE to the game's
   windows, like clicking X. Then wait :data:`CLOSE_GRACE_S`.
2. SIGINT to the ``xodus-cli`` process group (it forwards SIGINT to Wine).
   Then wait :data:`SIGINT_GRACE_S`.
3. :meth:`StagedStopper.force`: ``wineserver -k`` on this game's prefix
   only, then SIGTERM and SIGKILL to the group.

A second Stop jumps to step 3.

Every bounded wait is ``asyncio.wait``, never ``asyncio.wait_for``. On
3.10 ``wait_for`` raises ``asyncio.TimeoutError``, which is not the builtin
``TimeoutError`` there, so a handler written for 3.11 would let it escape.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from .runner import NO_WAIT_FLAG, STOP_FLAG

logger = logging.getLogger(__name__)

CLOSE_GRACE_S = 15.0
SIGINT_GRACE_S = 5.0
HELPER_TIMEOUT_S = 10.0
FORCE_STEP_S = 1.0
#: Steam's Stop sends SIGINT and SIGTERM within milliseconds. Signals this
#: close together are one Stop, not "stop, then force" (ISSUES.md I-034).
PAIRED_STOP_S = 1.0


async def wait_exit(proc: asyncio.subprocess.Process, timeout_s: float) -> bool:
    """Whether ``proc`` exits within ``timeout_s``. Never raises a timeout."""
    if proc.returncode is not None:
        return True
    waiter = asyncio.ensure_future(proc.wait())
    try:
        done, _pending = await asyncio.wait({waiter}, timeout=timeout_s)
    finally:
        if not waiter.done():
            waiter.cancel()
    return waiter in done


def signal_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """Send ``sig`` to ``proc``'s process group. False when it is gone.

    Every Xodus process is started with ``start_new_session=True``, so its
    group id is its pid.
    """
    if proc.returncode is not None:
        return False
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
    return True


async def kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL ``proc``'s group and wait for it, bounded."""
    signal_group(proc, signal.SIGKILL)
    await wait_exit(proc, FORCE_STEP_S * 5)


async def run_helper(
    argv: Sequence[str], env: Mapping[str, str], timeout_s: float = HELPER_TIMEOUT_S,
) -> int | None:
    """Run a short helper command. Its exit code, or None on failure or timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=dict(env), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as err:
        logger.warning("[xbox_pc.stop] %s did not start: %s", Path(argv[0]).name, err)
        return None
    try:
        if await wait_exit(proc, timeout_s):
            return proc.returncode
        logger.warning("[xbox_pc.stop] %s timed out after %.0fs", Path(argv[0]).name, timeout_s)
        await kill_and_reap(proc)
        return None
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise


class StagedStopper:
    """Stops one running ``xodus-cli run`` and the game under it."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        runner: Path,
        exe_name: str,
        env: Mapping[str, str],
        close_grace_s: float = CLOSE_GRACE_S,
        sigint_grace_s: float = SIGINT_GRACE_S,
    ) -> None:
        """``env`` is the helper env (no Steam identity, no service socket)."""
        self._proc = proc
        self._runner = runner
        self._exe_name = exe_name
        self._env = env
        self._close_grace_s = close_grace_s
        self._sigint_grace_s = sigint_grace_s

    async def staged(self) -> None:
        """WM_CLOSE, then SIGINT, then :meth:`force`. Returns once it exited."""
        if self._proc.returncode is not None:
            return
        if self._exe_name:
            logger.info("[xbox_pc.stop] asking %s to close its windows", self._exe_name)
            await run_helper(
                [str(self._runner), NO_WAIT_FLAG, "taskkill", "/IM", self._exe_name],
                self._env,
            )
            if await wait_exit(self._proc, self._close_grace_s):
                logger.info("[xbox_pc.stop] the game closed after WM_CLOSE")
                return
            logger.info("[xbox_pc.stop] the game did not close; sending SIGINT")
        if not signal_group(self._proc, signal.SIGINT):
            return
        if await wait_exit(self._proc, self._sigint_grace_s):
            logger.info("[xbox_pc.stop] the game stopped after SIGINT")
            return
        await self.force()

    async def force(self) -> None:
        """``wineserver -k`` on this prefix, then SIGTERM and SIGKILL the group."""
        if self._proc.returncode is not None:
            return
        logger.info("[xbox_pc.stop] forcing the stop: wineserver -k on this prefix only")
        await run_helper([str(self._runner), STOP_FLAG], self._env, timeout_s=5.0)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not signal_group(self._proc, sig):
                return
            if await wait_exit(self._proc, FORCE_STEP_S):
                return
        logger.warning("[xbox_pc.stop] xodus-cli survived SIGKILL (pid %d)", self._proc.pid)


async def stop_gracefully(stopper: StagedStopper, paired_s: float = PAIRED_STOP_S) -> NoReturn:
    """Run the staged stop; a later Stop request forces it.

    The caller was just cancelled once. Further cancels within ``paired_s``
    belong to the same Stop and are absorbed; one after that forces the stop.
    Always ends with the caller cancelled again, as the first Stop asked.
    """
    first = time.monotonic()
    staged = asyncio.ensure_future(stopper.staged())
    while not staged.done():
        try:
            await asyncio.wait({staged})
        except asyncio.CancelledError:
            if time.monotonic() - first < paired_s:
                logger.info("[xbox_pc.stop] stop signal pair; still closing the game")
                continue
            logger.info("[xbox_pc.stop] second stop request; forcing")
            staged.cancel()
            await asyncio.wait({staged})
            await stopper.force()
            break
    if staged.done() and not staged.cancelled() and staged.exception() is not None:
        logger.warning("[xbox_pc.stop] staged stop failed: %s", staged.exception())
        await stopper.force()
    raise asyncio.CancelledError

