"""The Origin client stand-in, run in the game's Wine session while it plays.

Older Origin SDK builds (2012-era titles) talk to LSX only while a process runs
at the registered ``ClientPath``; without one they wait at their loading screen
forever. ``maxima-cli`` carries a stand-in that only sleeps, installs it in the
prefix and points ``ClientPath`` at it (``unifideck-origin-standin``); the
sidecar's ``ready`` event names it. Measured on an EA title (2026-11-15): no LSX
connection without it, connected 10 s after launch with it.

Only games built with the Origin SDK get it. The SDK embeds the string
``OriginSDK`` in the exe, or ships beside it as its own library
(``OriginSDK.dll``; a Unity game keeps it under ``<Game>_Data/Managed``). A game
without it gains nothing and can be hurt: one EA build quits during start-up
while an Origin process runs, and reaches its menu without one (2026-11-15).
The exe's answer is kept in the game's install record, so a large exe is read
once; the library is looked for by name each launch.

A game can also start the client at ``ClientPath`` itself. When the stand-in is
not ours to run, ``reap_standin`` still ends such a copy once the game's exe
has gone: nothing else would, and the session would never end.

It is started inside the game's running session (``PROTON_VERB=runinprefix``)
once the game is up: started first, it would hold the Wine server and the
game's ``waitforexitandrun`` would wait for it. It is stopped as soon as the
game's own exe has exited, its Wine process included, so it never keeps the
session alive (it did once: the game had crashed, Steam still showed it running).

Standard library only: the launcher runs on the system Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import mmap
import os
import signal
from pathlib import Path

from .records import read_record, write_record

logger = logging.getLogger(__name__)

START_DELAY_S = 3.0
POLL_S = 2.0
#: The stand-in's file name, as ``unifideck-origin-standin`` installs it.
STANDIN_EXE = "Origin.exe"
#: How long the stand-in's umu run gets to return once its Wine process is gone.
STOP_WAIT_S = 15.0


_SDK_MARKERS = (b"OriginSDK", b"originsdk")


def embeds_origin_sdk(exe: Path) -> bool:
    """Whether ``exe`` was built with the Origin SDK (it carries the SDK's name)."""
    try:
        with exe.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return any(data.find(marker) >= 0 for marker in _SDK_MARKERS)
    except (OSError, ValueError):
        return False


#: The SDK shipped as a library of its own.
_SDK_LIBRARY_PREFIX = "originsdk"
#: Folders below the game's own that are searched for it.
_SDK_LIBRARY_DEPTH = 4


def ships_library(folder: Path, prefix: str) -> bool:
    """Whether a ``<prefix>*.dll`` (any case) lies within a few levels of ``folder``."""
    base = len(folder.parts)
    for root, dirs, files in os.walk(folder):
        if len(Path(root).parts) - base >= _SDK_LIBRARY_DEPTH:
            dirs.clear()
        for name in files:
            lower = name.lower()
            if lower.startswith(prefix) and lower.endswith(".dll"):
                return True
    return False


def ships_origin_sdk_library(folder: Path) -> bool:
    """Whether ``folder`` holds the Origin SDK as a library (``OriginSDK*.dll``)."""
    return ships_library(folder, _SDK_LIBRARY_PREFIX)


def wants_standin(game_id: str, exe: Path) -> bool:
    """Whether this game needs the stand-in: its exe or a library beside it is the Origin SDK.

    The exe is measured once and kept in the game's record.
    """
    record = read_record(game_id)
    if record is not None and record.sdk_exe == str(exe) and record.origin_sdk in ("yes", "no"):
        embedded = record.origin_sdk == "yes"
    else:
        embedded = embeds_origin_sdk(exe)
        if record is not None:
            with contextlib.suppress(OSError):
                write_record(dataclasses.replace(record, sdk_exe=str(exe), origin_sdk="yes" if embedded else "no"))
    folder = Path(record.install_path) if record is not None and record.install_path else exe.parent
    library = not embedded and ships_origin_sdk_library(folder)
    wanted = embedded or library
    logger.info(
        "[EA] %s: Origin SDK %s: stand-in %s", exe.name,
        "in the exe" if embedded else "as a library" if library else "not found", "on" if wanted else "off",
    )
    return wanted


def prefix_pids(exe_name: str, prefix: Path) -> list[int]:
    """Processes of this prefix running ``exe_name`` (Wine shows a Windows path)."""
    wanted = exe_name.lower()
    exact = f"WINEPREFIX={prefix}".encode()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            if cmd.replace("\\", "/").rsplit("/", 1)[-1].lower() != wanted:
                continue
            env = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
            if any(item == exact or item.startswith(exact + b"/") for item in env):
                found.append(int(entry))
        except OSError:
            continue
    return found


def game_exe_running(exe_name: str, prefix: Path) -> bool:
    """Whether a process of this prefix runs ``exe_name``."""
    return bool(prefix_pids(exe_name, prefix))


def _end_standin_processes(prefix: Path) -> None:
    """End the stand-in's Wine process itself.

    Killing umu's process group does not reach it: Wine starts it under the
    prefix's wineserver, outside that group. Left running, it holds the Wine
    session open, so Proton waits forever and Steam shows the game running.
    """
    for pid in prefix_pids(STANDIN_EXE, prefix):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


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
        await _stop(proc, prefix)


async def reap_standin(exe_name: str, prefix: Path, game_done: asyncio.Event) -> None:
    """End a stand-in the game started itself, once the game's exe has gone.

    For a game that does not get our stand-in. Its own copy, left running,
    holds the Wine session open after the game has quit.
    """
    seen = False
    while not game_done.is_set():
        running = await asyncio.to_thread(game_exe_running, exe_name, prefix)
        seen = seen or running
        if seen and not running and await asyncio.to_thread(game_exe_running, STANDIN_EXE, prefix):
            logger.warning(
                "[EA] %s quit and left an Origin client it started; ending it so the session can end", exe_name,
            )
            await asyncio.to_thread(_end_standin_processes, prefix)
            return
        await asyncio.sleep(POLL_S)


async def _stop(proc: asyncio.subprocess.Process, prefix: Path) -> None:
    """End the stand-in's Wine process; its umu run then returns on its own.

    Never SIGTERM the stand-in's umu while anything else runs in the prefix:
    umu answers a SIGTERM by tearing down the whole Wine session, and the game
    goes with it (exit 143). The stand-in also stops when its game exe has
    disappeared, which a game handing over to another exe looks like, so the
    session must survive this. The group kill stays as the last resort for a
    umu that has not returned.
    """
    await asyncio.to_thread(_end_standin_processes, prefix)
    try:
        await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
    except TimeoutError:
        logger.warning("[EA] stand-in's umu run did not return; ending its group")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
    logger.info("[EA] Origin client stand-in stopped")
