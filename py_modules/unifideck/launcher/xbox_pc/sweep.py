"""launcher/xbox_pc/sweep.py — remove what a killed launch left behind.

``xodus-cli run`` writes the decrypted executables into a launch view,
``.xodus-chain-*`` (or ``.xodus-offline-*``), in the parent of the game
folder, and deletes it when the game exits. A killed ``xodus-cli`` leaves
it on disk (I-019). A killed launcher can also leave its private service
folder.

Both sweeps are safe only while the caller holds this game's lock
(``component_locks.game_lock``): no other launch of the same game can then
be using a view or a session folder. That is why every function takes the
lock state and refuses without it. Only this game's own parent folder and
its own ``<PID>-*`` session folders are looked at, and a symlink is never
followed.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import logging
import shutil
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

VIEW_PREFIXES = (".xodus-chain-", ".xodus-offline-")


def _is_real_dir(path: Path) -> bool:
    """A folder that is not a symlink."""
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _remove(path: Path) -> bool:
    """Delete a real folder (rmtree never follows the links inside it)."""
    try:
        shutil.rmtree(path)
    except OSError as err:
        logger.warning("[xbox_pc.sweep] could not remove %s: %s", path, err)
        return False
    return True


def sweep_launch_views(game_dir: Path, *, lock_held: bool) -> list[str]:
    """Delete the launch views next to ``game_dir``. Returns their names.

    ``lock_held`` must be True: the caller holds this game's lock.
    """
    if not lock_held:
        raise RuntimeError("sweeping launch views needs the game lock")
    parent = game_dir.parent
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return []
    removed: list[str] = []
    for entry in entries:
        if not entry.name.startswith(VIEW_PREFIXES):
            continue
        if not _is_real_dir(entry):
            logger.warning("[xbox_pc.sweep] leaving %s: not a real folder", entry)
            continue
        if _remove(entry):
            logger.info("[xbox_pc.sweep] removed a stale launch view: %s", entry.name)
            removed.append(entry.name)
    return removed


def sweep_sessions(sessions: Path, pid: str, *, lock_held: bool) -> list[str]:
    """Delete this game's leftover service folders under ``sessions``."""
    if not lock_held:
        raise RuntimeError("sweeping service folders needs the game lock")
    if not _is_real_dir(sessions):
        return []
    removed: list[str] = []
    for entry in sorted(sessions.glob(f"{pid}-*")):
        if _is_real_dir(entry) and _remove(entry):
            removed.append(entry.name)
    return removed
