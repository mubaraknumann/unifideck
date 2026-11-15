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
import re
import signal
from pathlib import Path
from typing import Any

from . import maxima

logger = logging.getLogger(__name__)

#: The licence request and the prefix registry step happen before ``ready``.
READY_TIMEOUT_S = 180.0
#: Its log, redacted by the CLI, one per game so the last launch of each is kept.
#: A file, not a pipe: nobody reads a pipe during a long session, and a full one
#: would block the CLI and freeze LSX.
LOG_PATTERN = "launch-prepare-{}.log"
STOP_WAIT_S = 15.0
_TAIL_CHARS = 800
#: Older ``maxima-cli`` builds echoed EA's whole refusal, access token included.
_TOKEN_RE = re.compile(r'((?:Token|token)\s+value=")[^"]*|(Bearer\s+)\S+')
#: ``license request refused: CG_LIMIT_EXCEEDED`` (``unifideck-license-reuse``) or the older raw body.
_REFUSAL_RE = re.compile(r'license request refused: ([A-Z0-9_]+)|<error code="([A-Z0-9_]+)"')


class SidecarError(RuntimeError):
    """``launch-prepare`` ended before it was ready; ``detail`` is its output tail."""

    def __init__(self, returncode: int | None, detail: str) -> None:
        super().__init__(f"maxima-cli launch-prepare failed (exit {returncode}): {detail}")
        self.returncode = returncode
        self.detail = detail

    @property
    def licence_refusal(self) -> str | None:
        """EA's code when its licence service refused the game (``CG_LIMIT_EXCEEDED`` …)."""
        match = _REFUSAL_RE.search(self.detail)
        return (match.group(1) or match.group(2)) if match else None


class Sidecar:
    """One running ``launch-prepare``."""

    def __init__(self, proc: asyncio.subprocess.Process, env: dict[str, str], origin_standin: str | None) -> None:
        self._proc = proc
        self.env = env
        #: The Origin client stand-in in the prefix (``launcher/ea/standin.py``), or None.
        self.origin_standin = origin_standin

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
    proc, log_path = await _spawn(cli, offer_id, game_exe, env)
    try:
        ready = await asyncio.wait_for(_read_ready(proc), READY_TIMEOUT_S)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    if ready is None:
        raise SidecarError(proc.returncode, await asyncio.to_thread(_log_tail, log_path))
    env = {str(k): str(v) for k, v in ready["env"].items()}
    logger.info("[EA] licence and LSX ready for %s (port %s)", offer_id, env.get("EALsxPort"))
    standin = ready.get("origin_standin")
    return Sidecar(proc, env, standin if isinstance(standin, str) and standin else None)


async def _spawn(
    cli: Path, offer_id: str, game_exe: Path, env: dict[str, str],
) -> tuple[asyncio.subprocess.Process, Path]:
    """Start ``launch-prepare`` in its own session, logging to its per-game file."""
    # Imported here: ``launcher.proton`` imports the EA handler, which imports this module.
    from unifideck.launcher.proton.infrastructure.container_escape import escape_argv

    run_dir = await asyncio.to_thread(maxima.prepare_run_dir)
    log_path = run_dir.parent / LOG_PATTERN.format(re.sub(r"[^A-Za-z0-9._-]", "_", offer_id))
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
    return proc, log_path


async def _read_ready(proc: asyncio.subprocess.Process) -> dict[str, Any] | None:
    """The ``ready`` event, or None when stdout ends first."""
    assert proc.stdout is not None
    while line := await proc.stdout.readline():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "ready" and isinstance(event.get("env"), dict):
            return event
    await proc.wait()
    return None


def _log_tail(path: Path) -> str:
    try:
        tail = path.read_bytes()[-_TAIL_CHARS:].decode(errors="replace").strip()
    except OSError:
        return ""
    return _TOKEN_RE.sub(lambda m: (m.group(1) or m.group(2)) + "<redacted>", tail)
