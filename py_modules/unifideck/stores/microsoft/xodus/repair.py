"""stores/microsoft/xodus/repair.py — where a repair of an Xbox PC game goes.

Play on a game whose files are missing or incomplete (the launcher's
``not_downloaded``) queues an install into the same folder. The installer
treats an install over an existing record of the same root as a repair
(``installed``: every file is checked, the missing ones fetched) or a
resume (``partial``), so the repair only has to name that root's base.

Blocking file I/O; call it through ``asyncio.to_thread``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from unifideck.launcher.xbox_pc.layout import install_root_for

from .ids import record_for_game


def repair_target(game_id: str) -> dict[str, Any]:
    """``{base_path, title}`` of ``game_id``'s install, or ``{reason}``.

    Reasons: ``xbox_repair_no_record`` (nothing to repair: install it
    instead) and ``xbox_update_pending`` (an update must finish first).
    """
    record = record_for_game(game_id)
    if record is None:
        return {"reason": "xbox_repair_no_record"}
    if record.get("state") == "updating":
        return {"reason": "xbox_update_pending"}
    root = Path(str(record.get("installRoot") or ""))
    base = root.parent.parent
    pid = str(record.get("productId") or "")
    try:
        expected = install_root_for(base, pid)
    except ValueError:
        return {"reason": "xbox_repair_no_record"}
    if not root.is_absolute() or expected != root:
        # Not ``<base>/Xbox/<PID>``: an install over it would land elsewhere.
        return {"reason": "xbox_repair_no_record"}
    return {"base_path": str(base), "title": str(record.get("title") or "")}
