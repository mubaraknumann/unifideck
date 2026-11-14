"""``maxima-cli launch-prepare``, run beside an EA game for its whole session.

It requests the game's licence from EA, starts the LSX server (the local channel
EA's SDK in the game talks to instead of the EA app), and prints the launch
environment (``EALsxPort``, ``EALaunchUserAuthToken``, ``MXLaunchId`` …) as one
``{"event": "ready", "env": {…}}`` line. The launcher then starts the game
itself through umu with that environment, so Steam tracks the real game process
exactly as for every other store. Closing the sidecar's stdin ends it.

Standard library only: the launcher runs on the system Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from pathlib import Path

from . import maxima

logger = logging.getLogger(__name__)

#: The licence request and the prefix registry step happen before ``ready``.
READY_TIMEOUT_S = 180.0
#: Its log, redacted by the CLI. A file, not a pipe: nobody reads a pipe during
#: a long session, and a full one would block the CLI and freeze LSX.
LOG_NAME = "launch-prepare.log"
STOP_WAIT_S = 15.0
_TAIL_CHARS = 800


class SidecarError(RuntimeError):
    """``launch-prepare`` ended before it was ready; ``detail`` is its output tail."""

    def __init__(self, returncode: int | None, detail: str) -> None:
        super().__init__(f"maxima-cli launch-prepare failed (exit {returncode}): {detail}")
        self.returncode = returncode
        self.detail = detail


class Sidecar:
    """One running ``launch-prepare``."""

    def __init__(self, proc: asyncio.subprocess.Process, env: dict[str, str]) -> None:
        self._proc = proc
        self.env = env

    async def stop(self) -> None:
        """End the session: close stdin, then kill the group if it lingers."""
        if self._proc.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), STOP_WAIT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGKILL)
            await self._proc.wait()


async def start_sidecar(
    cli: Path, offer_id: str, game_exe: Path, env: dict[str, str],
) -> Sidecar:
    """Start ``launch-prepare`` and wait for its launch environment.

    ``env`` must carry ``maxima.base_env()`` and the game's ``prefix_env``.
    Raises :class:`SidecarError` when it exits or stalls before ``ready``.
    """
    # Imported here: ``launcher.proton`` imports the EA handler, which imports this module.
    from unifideck.launcher.proton.infrastructure.container_escape import escape_argv

    run_dir = await asyncio.to_thread(maxima.prepare_run_dir)
    log_path = run_dir.parent / LOG_NAME
    # It runs umu for the prefix registry step, which must not nest in Steam's container.
    argv = escape_argv(
        [str(cli), "launch-prepare", "--offer-id", offer_id, "--game-path", str(game_exe)], env, cwd=run_dir,
    )
    log = await asyncio.to_thread(log_path.open, "wb")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
            env=env, cwd=str(run_dir), start_new_session=True,
        )
    finally:
        log.close()  # the child holds its own descriptor
    try:
        ready = await asyncio.wait_for(_read_ready(proc), READY_TIMEOUT_S)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    if ready is None:
        raise SidecarError(proc.returncode, await asyncio.to_thread(_log_tail, log_path))
    logger.info("[EA] licence and LSX ready for %s (port %s)", offer_id, ready.get("EALsxPort"))
    return Sidecar(proc, ready)


async def _read_ready(proc: asyncio.subprocess.Process) -> dict[str, str] | None:
    """The ``ready`` environment, or None when stdout ends first."""
    assert proc.stdout is not None
    while line := await proc.stdout.readline():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "ready" and isinstance(event.get("env"), dict):
            return {str(k): str(v) for k, v in event["env"].items()}
    await proc.wait()
    return None


def _log_tail(path: Path) -> str:
    try:
        return path.read_bytes()[-_TAIL_CHARS:].decode(errors="replace").strip()
    except OSError:
        return ""
