"""stores/microsoft/xodus/installer.py — install one Xbox PC game.

``MicrosoftStore.install_game`` delegates here. The game goes to
``<base>/Xbox/<PID>/game``; ``<base>/Xbox/<PID>`` (the install root) is what
the queue, ``games.map``, the marker and an uninstall all name.

:meth:`XboxPcInstaller.precondition` is the cheap check the ``install_game``
RPC runs before queueing (no network, no process). :meth:`~XboxPcInstaller.install`
runs it again, because the queue may start the job much later.

An earlier record of the same game decides what an install means:

* ``installed`` in the same root: a repair. Streaming checks every file
  and fetches what is missing.
* ``installed`` in another root: refused, ``xbox_already_installed``.
* ``updating``: refused, ``xbox_update_pending``. The update must finish.
* ``partial`` in the same root: a resume.
* ``partial`` in another root: the old folder is deleted when the guards
  allow it, and the install starts fresh in the new root. When the guards
  refuse, or the old drive is not connected, the install is refused: the
  record is the only thing that still points at that folder.

The ownership marker is written before streaming starts. A cancel kills the
download and keeps the ``partial`` record, the marker and the files, so the
next install of the same game resumes and a destructive cleanup still finds
the folder. A repair holds the game lock for the whole download, so it
never streams over a running game and the game cannot start mid-repair.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import foreign_installs_under, safe_rmtree
from unifideck.core.types import InstallResult
from unifideck.launcher.xbox_pc.argv import validate_market
from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.install_records import RECORD_SCHEMA, read_install_record
from unifideck.launcher.xbox_pc.layout import game_dir_for, install_root_for

from . import install_steps as steps
from .errors import InstallError
from .guards import deletable, drive_missing, owner_key, streaming_active, symlink_supported
from .ids import product_id_for, store_game_id
from .progress import ProgressCallback

logger = logging.getLogger(__name__)

TitleLookup = Callable[[str], "dict[str, Any] | None"]


def plan_code(info: dict[str, Any]) -> str | None:
    """Why the library's package plan for a title rules out an install."""
    state = info.get("state")
    if state == "not_supported":
        return "xbox_pc_unsupported_plan"
    if state == "choice" and (info.get("reason") == "needs_choice" or not info.get("content_id")):
        # A choice row whose one supported Game sits beside add-ons already
        # names its package; ``select_content`` picks the same one.
        return "xbox_pc_needs_choice"
    if state == "ready" and not info.get("content_id"):
        return "xbox_pc_no_package"
    return None


def _same_path(a: Path, b: Path) -> bool:
    """Whether two paths name the same folder (symlinks resolved)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _drop_old_partial(old_root: Path, pid: str, game_id: str) -> None:
    """Delete an abandoned partial download in another root, or refuse.

    Overwriting the record would leave the old folder with nothing that
    points at it, so a folder that cannot be deleted, or a drive that is
    not connected, refuses the install instead.
    """
    if not old_root.exists():
        if drive_missing(old_root):
            raise InstallError("xbox_install_unreachable", str(old_root))
        return
    if deletable(old_root, pid, game_id) and safe_rmtree(old_root):
        logger.info("[xbox_install] removed the earlier partial download %s", old_root)
        return
    raise InstallError("xbox_old_partial_in_place", str(old_root))


def check_existing(existing: dict[str, Any] | None, root: Path, pid: str) -> str | None:
    """Apply an earlier record of ``pid``; return the content id to prefer.

    Blocking (it may delete an old partial folder): call through a thread.
    """
    if existing is None:
        return None
    state = existing.get("state")
    if state == "updating":
        raise InstallError("xbox_update_pending")
    old_root = Path(str(existing.get("installRoot") or ""))
    same = _same_path(old_root, root)
    if state == "installed" and not same:
        raise InstallError("xbox_already_installed", str(old_root))
    if state == "partial" and not same:
        _drop_old_partial(old_root, pid, store_game_id(existing))
        return None
    cid = existing.get("contentId")
    return cid if isinstance(cid, str) and cid else None


def prepare_root(root: Path, game_id: str) -> None:
    """Create ``root/game`` and check the folder can hold the game."""
    foreign = foreign_installs_under(root, owner_key=owner_key(game_id))
    if foreign:
        raise InstallError("xbox_folder_in_use", ", ".join(sorted(foreign)))
    try:
        game_dir_for(root).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise InstallError("mkdir_failed", e.strerror or type(e).__name__) from e
    if not symlink_supported(root):
        raise InstallError("xbox_filesystem_unsupported")


def _is_installed(record: dict[str, Any] | None) -> bool:
    """Whether ``record`` is a playable install (an install over it is a repair)."""
    return record is not None and record.get("state") == "installed"


async def _hold_game(stack: contextlib.ExitStack, pid: str, game_dir: Path) -> None:
    """Hold ``game-<PID>.lock`` on ``stack``, as an update does, or refuse."""
    if not stack.enter_context(game_lock(pid)):
        raise InstallError("xbox_game_running")
    if await asyncio.to_thread(streaming_active, game_dir):
        raise InstallError("xbox_install_in_progress")


@dataclass(frozen=True)
class InstallTarget:
    """What one install is for: both ids, the market and the title."""

    game_id: str
    pid: str
    market: str
    title: str


def partial_record(
    existing: dict[str, Any] | None, target: InstallTarget, cid: str, root: Path,
) -> dict[str, Any]:
    """The record to write before streaming starts.

    A repair keeps the ``installed`` record, so the game stays playable if
    the repair is cancelled. A resume keeps its first ``startedAt``.
    """
    names = {"market": target.market, "contentId": cid,
             "storeGameId": target.game_id, "title": target.title}
    if existing is not None and existing.get("state") == "installed":
        return dict(existing, **names)
    return {
        "schema": RECORD_SCHEMA, "productId": target.pid, "state": "partial",
        **names,
        "installRoot": str(root), "gameDir": str(game_dir_for(root)),
        "startedAt": (existing or {}).get("startedAt") or time.time(),
    }


class XboxPcInstaller:
    """Installs Xbox PC games through ``xodus-cli streaming``.

    ``title_for(game_id)`` is the library's ``metadata["xbox_pc"]`` dict of
    a title (stage B), or None when the title is not an Xbox PC title.
    """

    def __init__(
        self, xbox_pc: steps.XboxPcLike, cli: steps.XodusCliLike, title_for: TitleLookup,
    ) -> None:
        """Keep the facade, the CLI adapter and the library lookup."""
        self._xbox_pc = xbox_pc
        self._cli = cli
        self._title_for = title_for

    def _info(self, game_id: str) -> dict[str, Any] | None:
        """The library's Xbox PC facts of ``game_id``, or None."""
        if not isinstance(game_id, str) or not game_id:
            return None
        return self._title_for(game_id)

    async def precondition(self, game_id: str) -> str | None:
        """Why ``game_id`` cannot be queued now, or None. No network, no process."""
        info = self._info(game_id)
        if info is None or product_id_for(game_id, info) is None:
            return "not_supported"
        if not self._xbox_pc.enabled():
            return "xbox_pc_disabled"
        return plan_code(info) or await steps.ready_code(self._xbox_pc)

    def target(self, game_id: str) -> InstallTarget:
        """Both ids, the market and the title. Raises when the market is unknown."""
        info = self._info(game_id) or {}
        pid = product_id_for(game_id, info)
        if pid is None:
            raise InstallError("not_supported")
        for candidate in (info.get("market"), self._xbox_pc.market_hint()):
            try:
                market = validate_market(str(candidate or ""))
            except ValueError:
                continue
            return InstallTarget(game_id, pid, market, str(info.get("title") or ""))
        raise InstallError("xbox_market_unknown")

    async def install(
        self, game_id: str, base_path: str | None, progress_cb: ProgressCallback = None,
    ) -> InstallResult:
        """Install ``game_id`` under ``base_path``. Never raises except on cancel."""
        try:
            return await self._install(game_id, base_path, progress_cb)
        except InstallError as fail:
            logger.warning("[xbox_install] install of %s failed: %s", game_id, fail)
            return steps.failed_result(game_id, fail)
        except (OSError, ValueError) as e:
            # Our own failure. Its message can hold a home path, so only
            # the type reaches the user, as a coded error.
            logger.exception("[xbox_install] install of %s crashed", game_id)
            return steps.failed_result(game_id, InstallError("xodus_failed", type(e).__name__))

    async def _install(
        self, game_id: str, base_path: str | None, progress_cb: ProgressCallback,
    ) -> InstallResult:
        """The install steps, in order."""
        blocked = await self.precondition(game_id)
        if blocked:
            raise InstallError(blocked)
        await steps.report_starting(progress_cb)
        if not base_path:
            raise InstallError("install_path_not_found")
        target = self.target(game_id)
        await steps.check_backend(self._cli)
        root = install_root_for(base_path, target.pid)
        record = await self._prepare(target, root)
        return await self._download(record, root, progress_cb)

    async def _prepare(self, target: InstallTarget, root: Path) -> dict[str, Any]:
        """Settle an earlier record, pick the package, write the start record."""
        existing = await asyncio.to_thread(read_install_record, target.pid)
        prefer = await asyncio.to_thread(check_existing, existing, root, target.pid)
        cid = await steps.choose_content(self._cli, target.pid, target.market, prefer)
        await asyncio.to_thread(prepare_root, root, target.game_id)
        if not _is_installed(existing):
            # A repair keeps the marker commit wrote, which names the exe.
            await steps.mark_owned(root, target.game_id, target.title)
        record = partial_record(existing, target, cid, root)
        await steps.save_record(record)
        return record

    async def _download(
        self, record: dict[str, Any], root: Path, progress_cb: ProgressCallback,
    ) -> InstallResult:
        """Stream, verify, then commit. A repair holds the game lock throughout."""
        pid, game_dir = str(record["productId"]), game_dir_for(root)
        with contextlib.ExitStack() as stack:
            if _is_installed(record):
                await _hold_game(stack, pid, game_dir)
            outcome = await steps.stream_into(
                self._cli, pid, game_dir, str(record["market"]),
                str(record["contentId"]), progress_cb,
            )
            facts = await steps.verify(outcome, game_dir)
            return await steps.commit(record, facts, root, stamp="installedAt")
