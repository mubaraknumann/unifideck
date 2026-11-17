"""stores/microsoft/xodus/dlc_job.py — the DLC half of an Xbox PC install or Update job.

Every DLC download runs inside a download-queue job, so it has a row,
progress and a history entry like any download:

- **Install:** once the game is committed, :meth:`DlcJob.after_install`
  writes the game's DLC map (first install only); the DLC then downloads
  in its own Update job, which the frontend queues when the install
  completes, so cancelling it never undoes the install. The
  game page's DLC list and the install picker save what is ticked before
  install (:func:`save_choice`), small and large alike. With nothing saved,
  small DLC (``pc_library.dlc.LARGE_DLC_BYTES`` or less) is selected and
  large DLC declined. DLC bought later is never added here: it is offered
  on the game page's DLC list.
- **Update:** the game page shows Update while any chosen DLC is not
  installed. :meth:`DlcJob.run` downloads it, after the game's own update
  when there is one.

The job holds the game lock, so the game cannot start mid-download. A DLC
that fails stays pending (Play stays Update) and the job fails with
``xbox_dlc_failed`` naming it, so the user is told; the game itself is
never touched.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Protocol

from unifideck.core.types import InstallResult
from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.dlc_records import DLC_KEY, pending_dlc, with_dlc_state
from unifideck.launcher.xbox_pc.layout import (
    read_json_dict,
    state_dir,
    validate_pid,
    write_private_json,
)
from unifideck.stores.microsoft.pc_library.dlc import DlcInfo

from . import install_steps as steps
from .dlc_installer import XboxPcDlcInstaller
from .errors import InstallError
from .guards import streaming_active
from .ids import record_for_game
from .progress import ProgressCallback
from .updater import located_record

logger = logging.getLogger(__name__)

class DlcLookup(Protocol):
    """``OwnedDlc.for_game``: the owned DLC of a game, or None when unknown."""

    def __call__(self, game_id: str, *, force: bool = False) -> Awaitable[list[DlcInfo] | None]: ...

_CHOICE_DIR = "dlc-choice"


def _choice_path(pid: str) -> Path:
    return state_dir() / _CHOICE_DIR / f"{validate_pid(pid)}.json"


def save_choice(pid: str, ticked: list[str]) -> None:
    """Keep the DLC ticked for ``pid`` before it is installed. Blocking.

    ``ticked`` is every DLC to install, small or large: the game page's DLC
    list or the install picker saved it, and the install applies it.
    """
    folder = state_dir() / _CHOICE_DIR
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private_json(_choice_path(pid), {"ticked": sorted({p.upper() for p in ticked})})


def read_choice(pid: str) -> set[str] | None:
    """The DLC ticked for ``pid`` before install, or None when nothing was saved. Blocking."""
    data = read_json_dict(_choice_path(pid))
    ticked = data.get("ticked") if data else None
    return {str(p) for p in ticked} if isinstance(ticked, list) else None


def drop_choice(pid: str) -> None:
    """Forget the saved choice (it is in the record now). Blocking."""
    _choice_path(pid).unlink(missing_ok=True)


def initial_dlc(
    record: dict[str, Any], owned: list[DlcInfo], ticked: set[str] | None,
) -> dict[str, Any]:
    """``record`` with its first DLC map: the ticked DLC selected, the rest declined.

    With nothing saved (``ticked`` None) only the small DLC is selected.
    """
    out = dict(record)
    out.setdefault(DLC_KEY, {})
    for dlc in owned:
        wanted = dlc.product_id in ticked if ticked is not None else not dlc.large
        out = with_dlc_state(out, dlc.product_id, "selected" if wanted else "declined",
                             title=dlc.title, sizeBytes=dlc.size_bytes)
    return out


class DlcJob:
    """Downloads a game's chosen DLC inside an install or Update job."""

    def __init__(
        self, xbox_pc: steps.XboxPcLike, cli: steps.XodusCliLike, dlc_for: DlcLookup,
    ) -> None:
        """Keep the facade, the CLI and the owned-DLC lookup."""
        self._xbox_pc = xbox_pc
        self._dlc_for = dlc_for
        self._installer = XboxPcDlcInstaller(cli)
        self._cli = cli

    async def after_install(self, game_id: str, base: InstallResult) -> InstallResult:
        """Write the first DLC map of a new install. Downloads nothing.

        The install job ends here, so the game is installed (shortcut,
        ``games.map``) before any DLC streams: a cancelled DLC download then
        leaves an installed game whose Play shows Update. The frontend
        queues the DLC as its own Update job as soon as the install
        completes (``install_xbox_pc_dlc``).
        """
        if not base.success:
            return base
        record = await asyncio.to_thread(record_for_game, game_id)
        if record is None:
            return base
        pid = str(record["productId"])
        if DLC_KEY not in record:
            owned = await self._dlc_for(game_id)
            if owned is None:
                logger.warning(
                    "[xbox_dlc] %s: the account's DLC could not be read; none was installed. "
                    "It can be added from the game page's DLC list", pid,
                )
                return base
            chosen = await asyncio.to_thread(read_choice, pid)
            await steps.save_record(initial_dlc(record, owned, chosen))
            await asyncio.to_thread(drop_choice, pid)
        return base

    async def run(
        self, game_id: str, progress_cb: ProgressCallback = None, *, base: InstallResult | None = None,
    ) -> InstallResult:
        """Download every pending DLC of ``game_id``. Never raises except on cancel."""
        try:
            return await self._run(game_id, progress_cb, base)
        except InstallError as fail:
            logger.warning("[xbox_dlc] DLC of %s not downloaded: %s", game_id, fail)
            return steps.failed_result(game_id, fail)

    async def _run(
        self, game_id: str, progress_cb: ProgressCallback, base: InstallResult | None,
    ) -> InstallResult:
        record, root, game_dir = await asyncio.to_thread(located_record, game_id)
        if not pending_dlc(record):
            return base or self._done(game_id, root, record)
        blocked = await steps.ready_code(self._xbox_pc)
        if blocked:
            raise InstallError(blocked)
        await steps.check_backend(self._cli)
        with game_lock(str(record["productId"])) as held:
            if not held:
                raise InstallError("xbox_game_running")
            if await asyncio.to_thread(streaming_active, game_dir):
                raise InstallError("xbox_install_in_progress")
            _record, run = await self._installer.install_pending(record, root, progress_cb)
        if run.failed:
            raise InstallError("xbox_dlc_failed", ", ".join(run.failed_titles))
        return base or self._done(game_id, root, record)

    @staticmethod
    def _done(game_id: str, root: Path, record: dict[str, Any]) -> InstallResult:
        """The result of a DLC-only Update: the game as it was, still installed."""
        return InstallResult(
            success=True, store=steps.STORE, game_id=game_id, install_path=str(root),
            metadata={"exe_path": str(record.get("exe") or ""), "runtime_managed_prefix": True},
        )
