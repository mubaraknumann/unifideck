"""stores/microsoft/xodus/guards.py — may we touch this Xbox PC install now?

Every Xbox PC game lives in ``<base>/Xbox/<PID>/``. That folder (the install
root) is the unit of install, marker and deletion. The game files are in
its ``game/`` folder, and the launch views Xodus creates next to ``game/``
(``.xodus-chain-*``, ``.xodus-offline-*``, which hold decrypted executables)
are inside the root too, so deleting the root removes them.

Two locks tell us what is running:

* ``<gameDir>/.nexus-streaming.lock``: ``xodus-cli streaming`` holds it
  with ``flock`` for the whole download (``streaming_resume.rs``);
* ``locks/game-<PID>.lock``: the launcher holds it for the whole launch
  (``component_locks.game_lock``).

All functions do blocking I/O: call them through ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from unifideck.core.safe_delete import foreign_installs_under, is_safe_to_delete
from unifideck.launcher.proton.infrastructure.file_lock import try_exclusive
from unifideck.launcher.xbox_pc.layout import PID_RE

logger = logging.getLogger(__name__)

STREAMING_LOCK = ".nexus-streaming.lock"
INSTALL_PARENT = "Xbox"


def owner_key(game_id: str) -> str:
    """The ``games.map`` key of an Xbox PC title."""
    return f"microsoft:{game_id}"


def layout_ok(root: Path, pid: str) -> bool:
    """Whether ``root`` is ``<something>/Xbox/<PID>`` for this product id."""
    return bool(PID_RE.match(pid)) and root.name == pid and root.parent.name == INSTALL_PARENT


def deletable(root: Path, pid: str, game_id: str | None = None) -> bool:
    """Whether ``root`` may be deleted as ``pid``'s install.

    It must have our layout, pass the shared depth and home guard, and hold
    no other game. ``game_id`` is the title's ``store_game_id`` (its own
    ``games.map`` key); it defaults to the product id.
    """
    if not layout_ok(root, pid):
        logger.error("[xbox_install] %s is not an Xbox/<PID> folder of %s", root, pid)
        return False
    if not is_safe_to_delete(root):
        return False
    foreign = foreign_installs_under(root, owner_key=owner_key(game_id or pid))
    if foreign:
        logger.error("[xbox_install] %s also holds %s; not deleting it", root, foreign)
        return False
    return True


def drive_missing(root: Path) -> bool:
    """Whether the drive that held ``root`` (``<base>/Xbox/<PID>``) is not there.

    ``root`` itself is gone. When its ``<base>`` is gone too, the usual
    cause is an SD card or USB drive that is not mounted, so the folder may
    come back. Only a missing ``root`` under a present ``<base>`` is
    provably deleted.
    """
    return not root.parent.parent.is_dir()


def streaming_active(game_dir: Path) -> bool:
    """Whether an ``xodus-cli streaming`` run holds this game folder now."""
    return not try_exclusive(game_dir / STREAMING_LOCK)


def symlink_supported(folder: Path) -> bool:
    """Whether ``folder``'s filesystem can hold a symlink.

    ``xodus-cli run`` builds its launch view out of symlinks next to the
    game folder. exFAT and FAT32 cards cannot, and every launch would fail.
    """
    probe = folder / f".unifideck-symlink-{uuid.uuid4().hex[:8]}"
    try:
        os.symlink("game", probe)
    except OSError as e:
        logger.warning("[xbox_install] %s cannot hold symlinks: %s", folder, e)
        return False
    try:
        return probe.is_symlink()
    finally:
        probe.unlink(missing_ok=True)
