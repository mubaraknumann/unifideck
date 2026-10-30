"""Kill a silent installer that stops making progress.

py_modules/unifideck/stores/battlenet/prefix/stall_watchdog.py

Split from :mod:`.client_install`. The watchdog measures growth of the
prefix on disk, never the wall clock, so a slow download is left alone
while a wedged installer is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STALL_POLL_SECONDS = 15.0


def _tree_bytes(root: Path) -> int:
    """Total bytes under ``root``. Never raises; unreadable entries count 0."""
    total = 0
    stack = [root]
    while stack:
        size, children = _scan_dir(stack.pop())
        total += size
        stack.extend(children)
    return total


def _scan_dir(current: Path) -> tuple[int, list[Path]]:
    """``(bytes in this dir, subdirectories)``. An unreadable dir is ``(0, [])``.

    Split from :func:`_tree_bytes` so neither function nests past the cap;
    the swallow-everything behaviour is the point — this feeds a stall
    watchdog, and a permission error partway down a Wine prefix must read as
    "no growth here", never as an exception that kills the install.
    """
    size = 0
    children: list[Path] = []
    try:
        with os.scandir(current) as entries:
            for entry in entries:
                size += _entry_bytes(entry, children)
    except OSError:
        return 0, []
    return size, children


def _entry_bytes(entry: os.DirEntry[str], children: list[Path]) -> int:
    """Bytes for one entry, appending it to ``children`` when it is a dir."""
    try:
        if entry.is_dir(follow_symlinks=False):
            children.append(Path(entry.path))
            return 0
        if entry.is_file(follow_symlinks=False):
            return entry.stat().st_size
    except OSError:
        return 0
    return 0


async def stall_watchdog(prefix: Path, proc: asyncio.subprocess.Process, budget: float) -> None:
    """Kill ``proc`` once the prefix stops growing for ``budget`` seconds.

    A wall-clock cap cannot tell a frozen installer from a slow download —
    it kills both — so this measures *progress* instead. The freeze this
    exists for (a 32-bit client with no 32-bit Vulkan driver) writes nothing
    at all from the moment it wedges, while even a crawling download keeps
    adding bytes.
    """
    last = -1
    idle = 0.0
    while True:
        await asyncio.sleep(STALL_POLL_SECONDS)
        size = await asyncio.to_thread(_tree_bytes, prefix)
        if size != last:
            last, idle = size, 0.0
            continue
        idle += STALL_POLL_SECONDS
        if idle >= budget:
            logger.error(
                "[Battlenet] installer wrote nothing for %.0fs — stalled, killing it",
                budget,
            )
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            return
