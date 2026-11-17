"""stores/microsoft/xodus/dlc_choice.py — the game page's DLC list and what ticking does.

:func:`dlc_view` is the list ``get_xbox_pc_dlc`` answers: every owned,
installable DLC of the game with its state. :func:`apply_choice` is what
``set_xbox_pc_dlc`` does with the ticked product ids:

- **Game not installed:** the ticked DLC, small and large, is kept
  (``dlc_job.save_choice``); the install picker opens with it and the
  install applies it.
- **Game installed:** a ticked DLC that is not installed becomes
  ``selected`` (pending: Play turns into Update and nothing downloads until
  the user presses it). An unticked DLC becomes ``declined`` and its folder
  is deleted, which the game lock guards: never while the game runs or a
  download writes the folder.

Nothing here downloads.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import safe_rmtree
from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.dlc_records import dlc_dir_for, dlc_entries, with_dlc_state
from unifideck.stores.microsoft.pc_library.dlc import DlcInfo

from . import install_steps as steps
from .dlc_job import read_choice, save_choice
from .errors import InstallError
from .guards import streaming_active
from .ids import record_for_game
from .updater import located_record

logger = logging.getLogger(__name__)

#: DLC states that leave files in the DLC folder.
_HAS_FILES = frozenset({"installed", "partial", "failed"})


def dlc_view(
    owned: list[DlcInfo], record: dict[str, Any] | None, ticked: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Each owned DLC with ``state`` and ``chosen``, in ``owned`` order.

    For an installed game the state is the record's (``not_installed`` when
    absent). Before install it is ``not_installed``, and ``chosen`` is the
    saved choice (``ticked``), or every DLC when nothing was saved: the
    install picker then opens with all of them ticked.
    """
    installed = record is not None and record.get("state") in ("installed", "updating")
    entries = dlc_entries(record) if installed else {}
    out: list[dict[str, Any]] = []
    for dlc in owned:
        state = str(entries.get(dlc.product_id, {}).get("state") or "not_installed")
        if installed:
            chosen = state not in ("declined", "not_installed")
        else:
            chosen = ticked is None or dlc.product_id in ticked
        out.append({**dlc.to_dict(), "state": state, "chosen": chosen})
    return out


async def view_for(game_id: str, owned: list[DlcInfo]) -> dict[str, Any]:
    """The RPC answer: ``{installed, dlc: [...]}``."""
    record = await asyncio.to_thread(record_for_game, game_id)
    installed = record is not None and record.get("state") in ("installed", "updating")
    chosen = None
    if not installed and owned:
        chosen = await asyncio.to_thread(read_choice, owned[0].parent)
    return {"installed": installed, "dlc": dlc_view(owned, record, chosen)}


def _remove_folder(root: Path, pid: str) -> None:
    """Delete one DLC folder. Raises ``xbox_dlc_remove_failed``. Blocking."""
    folder = dlc_dir_for(root, pid)
    if streaming_active(folder):
        raise InstallError("xbox_install_in_progress")
    if not safe_rmtree(folder):
        raise InstallError("xbox_dlc_remove_failed", pid)


def _apply_installed(
    record: dict[str, Any], root: Path, owned: list[DlcInfo], ticked: set[str],
) -> dict[str, Any]:
    """The record after the choice; unticked folders deleted. Blocking."""
    entries = dlc_entries(record)
    for dlc in owned:
        pid, state = dlc.product_id, entries.get(dlc.product_id, {}).get("state")
        facts = {"title": dlc.title, "sizeBytes": dlc.size_bytes}
        if pid in ticked:
            if state != "installed":
                record = with_dlc_state(record, pid, "selected", **facts)
            continue
        if state in _HAS_FILES:
            _remove_folder(root, pid)
        record = with_dlc_state(record, pid, "declined", **facts)
    return record


async def apply_choice(game_id: str, owned: list[DlcInfo], ticked: list[str]) -> dict[str, Any]:
    """Apply the ticked product ids; ``{success, error?, installed}``.

    Ids that are not owned DLC of this game are ignored.
    """
    owned_ids = {d.product_id for d in owned}
    chosen = {p.upper() for p in ticked} & owned_ids
    found = await asyncio.to_thread(record_for_game, game_id)
    if found is None or found.get("state") not in ("installed", "updating"):
        if owned:
            await asyncio.to_thread(save_choice, owned[0].parent, sorted(chosen))
        return {"success": True, "installed": False}
    try:
        record, root, _game_dir = await asyncio.to_thread(located_record, game_id)
        with game_lock(str(record["productId"])) as held:
            if not held:
                raise InstallError("xbox_game_running")
            updated = await asyncio.to_thread(_apply_installed, record, root, owned, chosen)
            await steps.save_record(updated)
    except InstallError as fail:
        logger.warning("[xbox_dlc] choice for %s not applied: %s", game_id, fail)
        return {"success": False, "error": fail.code, "installed": True}
    logger.info("[xbox_dlc] %s: %d of %d DLC chosen", game_id, len(chosen), len(owned))
    return {"success": True, "installed": True}
