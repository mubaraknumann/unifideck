"""launcher/xbox_pc/restart_watch.py — a game that keeps restarting itself.

An anti-cheat bootstrapper whose anti-cheat cannot load starts the game, the
game finds no anti-cheat and starts the bootstrapper again, without end
(I-044). ``xodus-cli`` sees nothing wrong: the prefix never goes idle, so it
never exits, and the user only sees a game that never shows. This watch
counts how often each executable under the game's install folder starts.
Too many starts of one executable in a short window end the launch.

EasyAntiCheat's own loader log then says why. A failed download of its
Linux (``wine64``) client means the game's developer never enabled EAC for
Proton; nothing on the Deck can change that, and the user is told so.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import asyncio
import collections
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path

#: Starts of one executable within :data:`RESTART_WINDOW_S` that count as
#: a loop. A launcher that restarts its game once or twice stays well below.
RESTART_LIMIT = 8
RESTART_WINDOW_S = 90.0
POLL_S = 1.0
#: A loop runs one copy at a time, two while one hands over to the next.
#: An embedded browser runs many copies of one executable at once; their
#: starts are not counted.
MAX_ALIVE_IN_LOOP = 2

#: EasyAntiCheat's per-game logs, relative to the Wine prefix.
_EAC_LOG_GLOB = "drive_c/users/steamuser/AppData/Roaming/EasyAntiCheat/*/loader.log"
_EAC_LINUX_CLIENT = "/client/wine64/"
_EAC_CALLBACK_ERROR = re.compile(r"\[EAC Callback\] Code \d+")

ListProcesses = Callable[[], Mapping[int, str]]


def wine_path(path: Path) -> str:
    """``path`` as Wine shows it in a command line (``Z:\\...``), lower-case."""
    return ("z:" + str(path).replace("/", "\\")).lower()


def game_processes(install_dir: Path, proc_root: Path = Path("/proc")) -> dict[int, str]:
    """``pid -> executable name`` (lower-case) of processes run from ``install_dir``.

    A Wine process's ``argv[0]`` is its Windows path. ``install_dir`` holds
    the game folder and its launch views, and no other game's files.
    """
    folder = wine_path(install_dir) + "\\"
    found: dict[int, str] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv0 = (entry / "cmdline").read_bytes().split(b"\0", 1)[0]
        except OSError:
            continue
        command = argv0.decode("utf-8", errors="replace").lower()
        if command.startswith(folder):
            found[int(entry.name)] = command.rsplit("\\", 1)[-1]
    return found


class RestartCounter:
    """Counts new processes per executable name over a sliding window."""

    def __init__(self, limit: int = RESTART_LIMIT, window_s: float = RESTART_WINDOW_S) -> None:
        """``limit`` starts within ``window_s`` seconds make a loop."""
        self._limit = limit
        self._window_s = window_s
        self._seen: set[int] = set()
        self._starts: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)

    def observe(self, processes: Mapping[int, str], now: float) -> str | None:
        """Record ``processes``; the name of a looping executable, or None."""
        alive = collections.Counter(processes.values())
        for pid, name in processes.items():
            if pid in self._seen:
                continue
            self._seen.add(pid)
            if alive[name] > MAX_ALIVE_IN_LOOP:
                continue
            starts = self._starts[name]
            starts.append(now)
            while starts and now - starts[0] > self._window_s:
                starts.popleft()
            if len(starts) >= self._limit:
                return name
        return None


async def watch_restarts(
    list_processes: ListProcesses,
    counter: RestartCounter | None = None,
    poll_s: float = POLL_S,
) -> str:
    """Return the name of the first executable that restarts in a loop.

    Never returns otherwise: the caller cancels it when the game ends.
    """
    counter = counter or RestartCounter()
    while True:
        processes = await asyncio.to_thread(list_processes)
        looping = counter.observe(processes, time.monotonic())
        if looping is not None:
            return looping
        await asyncio.sleep(poll_s)


def eac_linux_client_refused(prefix: Path, since: float) -> bool:
    """Whether EasyAntiCheat failed to fetch its Linux client in this launch.

    Reads the loader logs in ``prefix`` written at or after ``since``
    (wall-clock seconds), so a log from an older launch never counts.
    """
    for log in prefix.glob(_EAC_LOG_GLOB):
        try:
            if log.stat().st_mtime < since:
                continue
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _EAC_LINUX_CLIENT in text and _EAC_CALLBACK_ERROR.search(text):
            return True
    return False
