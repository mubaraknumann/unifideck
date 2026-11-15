"""The Origin client stand-in, run in the game's Wine session while it plays.

Older Origin SDK builds (2012-era titles) talk to LSX only while a process runs
at the registered ``ClientPath``; without one they wait at their loading screen
forever. ``maxima-cli`` carries a stand-in that only sleeps, installs it in the
prefix and points ``ClientPath`` at it (``unifideck-origin-standin``); the
sidecar's ``ready`` event names it. Measured on an EA title (2026-11-15): no LSX
connection without it, connected 10 s after launch with it.

It is started inside the game's running session (``PROTON_VERB=runinprefix``)
once the game is up: started first, it would hold the Wine server and the
game's ``waitforexitandrun`` would wait for it. It is stopped as soon as the
game's own exe has exited, so it never keeps the session alive.

Standard library only: the launcher runs on the system Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

logger = logging.getLogger(__name__)

START_DELAY_S = 3.0
POLL_S = 2.0


def game_exe_running(exe_name: str, prefix: Path) -> bool:
    """Whether a process of this prefix runs ``exe_name`` (Wine shows a Windows path)."""
    wanted = exe_name.lower()
    exact = f"WINEPREFIX={prefix}".encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            if cmd.replace("\\", "/").rsplit("/", 1)[-1].lower() != wanted:
                continue
            env = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
            if any(item == exact or item.startswith(exact + b"/") for item in env):
                return True
        except OSError:
            continue
    return False


async def run_standin(
    argv: list[str], env: dict[str, str], exe_name: str, prefix: Path, game_done: asyncio.Event,
) -> None:
    """Start the stand-in once the game is up; stop it when the game's exe has gone.

    ``argv`` runs the stand-in through umu; ``game_done`` is set when the game's
    umu run has returned. Cancellation stops it too.
    """
    # Imported here: ``launcher.proton`` imports the EA handler, which imports this module.
    from unifideck.launcher.proton.infrastructure.container_escape import escape_argv

    await asyncio.sleep(START_DELAY_S)
    if game_done.is_set():
        return
    env = {**env, "PROTON_VERB": "runinprefix"}
    proc = await asyncio.create_subprocess_exec(
        *escape_argv(argv, env), env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    logger.info("[EA] Origin client stand-in started")
    try:
        seen = False
        while not game_done.is_set():
            running = await asyncio.to_thread(game_exe_running, exe_name, prefix)
            seen = seen or running
            if seen and not running:
                break
            await asyncio.sleep(POLL_S)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
        logger.info("[EA] Origin client stand-in stopped")
