"""stores/microsoft/xodus/uninstaller.py — remove one Xbox PC game.

Uninstall is NOT gated by the feature toggle, the components or the sign-in.
A user who turned the feature off, or never finished installing the
components again, must still get their disk space back.

The install root is found from the record, else from the ownership marker
(``.unifideck_manifest.json`` in the root). Deleting the root removes the
game folder and every launch view Xodus created next to it.

A record whose folder is missing is forgotten only when the folder is
provably gone: its ``<base>`` is still there. When the ``<base>`` is missing
too (an SD card that is not mounted, for instance), the uninstall refuses
with ``xbox_install_unreachable`` and keeps the record. Forgetting it then
would leave the game files with nothing that points at them once the card
is back, and the caller would delete the prefix too.

A title with neither a record nor a marker was never installed here (a
stream-only title, for instance). That is ``not_supported``, never a
success, so the shortcut does not flip out of a state it never held.

The Proton prefix is not touched here. The uninstall RPC deletes it when
the user asked for that (``core.safe_delete.delete_game_prefix``).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.core import marker_sweep
from unifideck.core.safe_delete import safe_rmtree
from unifideck.core.types import Events, Result
from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.install_records import remove_install_record
from unifideck.launcher.xbox_pc.layout import game_dir_for

from .errors import InstallError
from .guards import deletable, drive_missing, layout_ok, streaming_active
from .ids import as_pid, record_for_game

logger = logging.getLogger(__name__)

STORE = "microsoft"
NOT_SUPPORTED = "not_supported"


@dataclass(frozen=True)
class Located:
    """What an uninstall found: the record, the product id and the folder.

    ``root`` is None when no folder of ours is on disk. ``pid`` is None when
    nothing at all names this title.
    """

    record: dict[str, Any] | None
    pid: str | None
    root: Path | None


def _record_root(record: dict[str, Any]) -> Path | None:
    """The record's install root when it has our layout and is on disk."""
    root = Path(str(record.get("installRoot") or ""))
    ok = layout_ok(root, str(record.get("productId"))) and root.is_dir()
    return root if ok else None


def locate(game_id: str) -> Located:
    """Find ``game_id``'s install from its record, else its marker. Blocking."""
    record = record_for_game(game_id)
    if record is not None:
        root = _record_root(record)
        if root is not None:
            return Located(record, str(record["productId"]), root)
    marked = marker_sweep.find_for_game(marker_sweep.collect_install_roots(), STORE, game_id)
    pid = str(record["productId"]) if record is not None else as_pid(marked.name if marked else game_id)
    if marked is not None and pid is not None and layout_ok(marked, pid):
        return Located(record, pid, marked)
    return Located(record, pid, None)


def delete_root(root: Path, pid: str, game_id: str) -> None:
    """Delete ``root`` after the guards. Blocking."""
    if not deletable(root, pid, game_id):
        raise InstallError("uninstall_refused_unsafe_path")
    if not safe_rmtree(root):
        raise InstallError("uninstall_incomplete_files_remain")


class XboxPcUninstaller:
    """Uninstalls Xbox PC games. ``bus`` receives ``GAME_UNINSTALLED``."""

    def __init__(self, bus: Any) -> None:
        """Keep the event bus."""
        self._bus = bus

    async def uninstall(self, game_id: str) -> Result:
        """Uninstall ``game_id``. Never raises except on cancel."""
        if not isinstance(game_id, str) or not game_id:
            return self._failed(InstallError(NOT_SUPPORTED))
        try:
            return await self._uninstall(game_id)
        except InstallError as fail:
            logger.warning("[xbox_install] uninstall of %s refused: %s", game_id, fail)
            return self._failed(fail)

    async def _uninstall(self, game_id: str) -> Result:
        """Find the folder, check the locks, delete, then forget the game."""
        found = await asyncio.to_thread(locate, game_id)
        if found.pid is None or (found.root is None and found.record is None):
            raise InstallError(NOT_SUPPORTED)
        if found.root is None:
            return await self._forget_missing(game_id, found)
        with game_lock(found.pid) as held:
            if not held:
                raise InstallError("xbox_game_running")
            if await asyncio.to_thread(streaming_active, game_dir_for(found.root)):
                raise InstallError("xbox_install_in_progress")
            await asyncio.to_thread(delete_root, found.root, found.pid, game_id)
        logger.info("[xbox_install] uninstalled %s from %s", game_id, found.root)
        return await self._forget(game_id, found.pid)

    async def _forget_missing(self, game_id: str, found: Located) -> Result:
        """Forget a record whose folder is gone, unless its drive is just away."""
        assert found.record is not None and found.pid is not None
        recorded = Path(str(found.record.get("installRoot") or ""))
        if recorded.is_absolute() and await asyncio.to_thread(drive_missing, recorded):
            raise InstallError("xbox_install_unreachable", str(recorded))
        # The folder was deleted by hand, or the card was wiped. Forgetting
        # the record is the whole uninstall.
        logger.info("[xbox_install] %s has a record but no folder; forgetting it", game_id)
        return await self._forget(game_id, found.pid)

    async def _forget(self, game_id: str, pid: str) -> Result:
        """Drop the record and tell the plugin the game is gone."""
        await asyncio.to_thread(remove_install_record, pid)
        await self._bus.emit(Events.GAME_UNINSTALLED, store=STORE, game_id=game_id)
        return Result(success=True, store=STORE)

    @staticmethod
    def _failed(fail: InstallError) -> Result:
        """The failed ``Result`` for ``fail``."""
        return Result(success=False, store=STORE, error=str(fail), error_code=fail.code)
