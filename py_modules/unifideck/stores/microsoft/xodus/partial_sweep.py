"""stores/microsoft/xodus/partial_sweep.py — discard abandoned Xbox PC downloads.

A cancelled or failed install keeps its ``partial`` record and files, so the
next install of that game resumes. Nobody may ever come back for it, and the
files can be many GB, so a partial nobody touched for
:data:`PARTIAL_MAX_AGE_S` is deleted.

The age is the record file's mtime: the installer writes the record when a run
starts and touches it when the run ends.

Only ``partial`` records are ever considered. An ``installed`` game (even one
whose repair was cancelled) and an ``updating`` one are never touched. Also
kept: a game in the download queue or still streaming, and a folder on a drive
that is not connected, since the record is all that still points at it.

Not gated by the feature toggle or the sign-in: a user who turned Xbox PC off
still gets the disk space back.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import safe_rmtree
from unifideck.launcher.xbox_pc.install_records import (
    list_install_records,
    record_path,
    remove_install_record,
)
from unifideck.launcher.xbox_pc.layout import game_dir_for

from .guards import deletable, drive_missing, layout_ok, streaming_active
from .ids import store_game_id

logger = logging.getLogger(__name__)

PARTIAL_MAX_AGE_S = 24 * 3600
_LOG = "[xbox_install]"


def _idle_for(pid: str, now: float) -> float | None:
    """Seconds since this record was last touched, or None when unreadable."""
    try:
        return now - record_path(pid).stat().st_mtime
    except OSError:
        return None


def _discard(record: dict[str, Any]) -> bool:
    """Delete one abandoned partial and its record. True when it was deleted."""
    pid = str(record["productId"])
    game_id = store_game_id(record)
    root = Path(str(record.get("installRoot") or ""))
    if not layout_ok(root, pid):
        logger.warning("%s keeping partial %s: unexpected folder %s", _LOG, pid, root)
        return False
    if not root.exists():
        if drive_missing(root):
            return False
        remove_install_record(pid)
        logger.info("%s forgot partial %s: %s is already gone", _LOG, pid, root)
        return False
    if streaming_active(game_dir_for(root)):
        return False
    if not deletable(root, pid, game_id) or not safe_rmtree(root):
        logger.warning("%s could not discard partial %s at %s; will retry", _LOG, pid, root)
        return False
    remove_install_record(pid)
    logger.info("%s discarded abandoned partial download %s at %s", _LOG, pid, root)
    return True


def discard_stale_partials(active_game_ids: frozenset[str], now: float | None = None) -> list[str]:
    """Discard every abandoned partial; return the product ids deleted. Blocking."""
    moment = time.time() if now is None else now
    records, _unreadable = list_install_records()
    discarded: list[str] = []
    for record in records:
        if record.get("state") != "partial" or store_game_id(record) in active_game_ids:
            continue
        pid = str(record["productId"])
        idle = _idle_for(pid, moment)
        if idle is not None and idle >= PARTIAL_MAX_AGE_S and _discard(record):
            discarded.append(pid)
    return discarded


class PartialSweepService:
    """Runs :func:`discard_stale_partials` after boot, then every hour.

    ``active_game_ids`` names the Xbox games in the download queue right now.
    """

    BOOT_DELAY_SECONDS = 60
    POLL_INTERVAL_SECONDS = 3600

    def __init__(self, active_game_ids: Callable[[], frozenset[str]]) -> None:
        """Keep the queue lookup."""
        self._active_game_ids = active_game_ids
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Begin the loop. Starting twice does nothing."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="xbox_partial_sweep")

    async def stop(self) -> None:
        """Cancel the loop. Never raises."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def sweep(self) -> list[str]:
        """One pass, off the event loop."""
        return await asyncio.to_thread(discard_stale_partials, self._active_game_ids())

    async def _loop(self) -> None:
        await asyncio.sleep(self.BOOT_DELAY_SECONDS)
        while True:
            try:
                await self.sweep()
            except Exception:
                # Losing the sweep costs disk space the user expects back.
                logger.exception("%s abandoned download sweep failed; retrying in 1 h", _LOG)
            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
