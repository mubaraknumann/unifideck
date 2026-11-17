"""stores/microsoft/xodus/dlc_installer.py — download the DLC an Xbox PC game is waiting for.

Runs inside a download-queue job, after the base game (an install) or
instead of it (an Update whose only pending work is DLC). Every DLC whose
record entry is pending (``selected``, ``partial``, ``failed``) is
streamed, in product-id order, into ``<installRoot>/dlc/<PID>``:

1. ``resolve-package <DLC> --include-dlc``, then the one PC ``Durable``
   choice whose parent is this game (``select_dlc_content``);
2. the entry goes to ``partial`` before the first byte;
3. ``xodus-cli streaming`` through the same steps as a game
   (``install_steps.stream_into``: credential lock, component locks,
   progress to the queue row);
4. a finished run with the package files in place makes it ``installed``;
   anything else makes it ``failed``.

One DLC failing does not stop the others, and never touches the game:
the run reports which ones failed, and they stay pending, so Play stays
Update until they download or the user unticks them. A cancel stops at
once and leaves the current entry ``partial``; the next Update resumes it
from the CLI's journal.

The caller holds the game lock, so the game cannot start mid-download.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unifideck.launcher.xbox_pc.argv import validate_market
from unifideck.launcher.xbox_pc.dlc_records import (
    dlc_dir_for,
    dlc_entries,
    pending_dlc,
    with_dlc_state,
)

from . import install_steps as steps
from .errors import InstallError, classify_failure, cli_error_text
from .package_choice import select_dlc_content
from .progress import ProgressCallback

logger = logging.getLogger(__name__)

#: Files a finished DLC package folder holds (compared without case).
_PACKAGE_FILES = frozenset({"microsoftgame.config", "appxmanifest.xml"})


@dataclass
class DlcRun:
    """What one pass over the pending DLC did."""

    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: Titles of the failed DLC, for the notice.
    failed_titles: list[str] = field(default_factory=list)


def package_in_place(folder: Path) -> bool:
    """Whether ``folder`` holds a DLC package's config or manifest. Blocking."""
    try:
        return any(p.name.lower() in _PACKAGE_FILES for p in folder.iterdir() if p.is_file())
    except OSError:
        return False


def _mkdir(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise InstallError("mkdir_failed", e.strerror or type(e).__name__) from e


class XboxPcDlcInstaller:
    """Streams the pending DLC of one installed game."""

    def __init__(self, cli: steps.XodusCliLike) -> None:
        """Keep the CLI adapter."""
        self._cli = cli

    async def install_pending(
        self, record: dict[str, Any], root: Path, progress_cb: ProgressCallback = None,
    ) -> tuple[dict[str, Any], DlcRun]:
        """Stream every pending DLC of ``record``. Returns the new record and the run.

        The record is saved after each step, so a crash or a cancel leaves
        a truthful state. Raises only on cancel.
        """
        run = DlcRun()
        pending = pending_dlc(record)
        if not pending:
            return record, run
        parent = str(record["productId"])
        market = validate_market(str(record.get("market") or ""))
        logger.info("[xbox_dlc] %s: %d DLC to download", parent, len(pending))
        for pid in pending:
            record = await self._one(record, root, parent, pid, market, progress_cb, run)
        return record, run

    async def _one(
        self, record: dict[str, Any], root: Path, parent: str, pid: str, market: str,
        progress_cb: ProgressCallback, run: DlcRun,
    ) -> dict[str, Any]:
        """Stream one DLC and record how it ended."""
        title = str(dlc_entries(record).get(pid, {}).get("title") or pid)
        try:
            record = await self._stream(record, root, parent, pid, market, progress_cb)
        except InstallError as fail:
            logger.warning("[xbox_dlc] %s DLC %s failed: %s", parent, pid, fail)
            record = with_dlc_state(record, pid, "failed", error=fail.code)
            await steps.save_record(record)
            run.failed.append(pid)
            run.failed_titles.append(title)
            return record
        run.installed.append(pid)
        logger.info("[xbox_dlc] %s DLC %s installed", parent, pid)
        return record

    async def _stream(
        self, record: dict[str, Any], root: Path, parent: str, pid: str, market: str,
        progress_cb: ProgressCallback,
    ) -> dict[str, Any]:
        """Resolve, mark ``partial``, stream, check, mark ``installed``."""
        cid = await self._content_id(pid, parent, market)
        folder = dlc_dir_for(root, pid)
        await asyncio.to_thread(_mkdir, folder)
        record = with_dlc_state(record, pid, "partial", contentId=cid)
        await steps.save_record(record)
        await steps.report_starting(progress_cb)
        outcome = await steps.stream_into(self._cli, pid, folder, market, cid, progress_cb)
        if not outcome.succeeded:
            if outcome.terminal == "complete":
                raise InstallError("xbox_dlc_incomplete", f"exit_after_complete | {outcome.tail}")
            raise classify_failure(outcome)
        if not await asyncio.to_thread(package_in_place, folder):
            raise InstallError("xbox_dlc_incomplete", "no package files")
        record = with_dlc_state(record, pid, "installed", contentId=cid, error=None)
        await steps.save_record(record)
        return record

    async def _content_id(self, pid: str, parent: str, market: str) -> str:
        """The DLC package of ``pid`` that belongs to ``parent``. Raises ``InstallError``."""
        try:
            plan = await self._cli.resolve_package(pid, market, include_dlc=True)
        except Exception as e:
            raise InstallError("xbox_resolve_failed", cli_error_text(e)) from e
        return select_dlc_content(plan, pid, parent)
