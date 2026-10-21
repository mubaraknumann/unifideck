"""launcher/xbox_pc/component_locks.py — who may use or delete a component.

Three kinds of lock, all ``fcntl.flock`` on files under ``locks/`` or the
root, so they work across the plugin and every launcher process:

* ``install.lock``. A component install holds it exclusively only for the
  short promote step (rename, ``active.json``, GC). Readers hold it shared
  just long enough to read ``active.json`` and take their component locks.
  The 1 to 2 minute extract runs with no lock held, so a launch never waits
  for it.
* ``backend-<dir>.lock`` and ``runtime-<dir>.lock``. A launch holds both
  shared for its whole session. GC deletes a folder only when it can take
  that folder's lock exclusively, so a running game keeps its runtime.
* ``game-<PID>.lock``. Held exclusively by the launch of that game. An
  update or uninstall that cannot take it refuses with "game running".

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from unifideck.launcher.proton.infrastructure.file_lock import flock_path, try_exclusive

from .layout import (
    SHARED_LOCK_WAIT_S,
    STAGING_PREFIX,
    ActiveComponents,
    backend_root,
    data_root,
    ensure_private_dir,
    install_lock_path,
    locks_dir,
    read_active,
    runtime_root,
    validate_pid,
)

logger = logging.getLogger(__name__)


class ComponentsBusyError(RuntimeError):
    """A component lock could not be taken in time (an install is promoting)."""


#: The name the shared contract uses.
ComponentsBusy = ComponentsBusyError


def backend_lock_path(dirname: str) -> Path:
    """The session lock of ``backend/<dirname>``."""
    return locks_dir() / f"backend-{dirname}.lock"


def runtime_lock_path(dirname: str) -> Path:
    """The session lock of ``runtime/<dirname>``."""
    return locks_dir() / f"runtime-{dirname}.lock"


def game_lock_path(pid: str) -> Path:
    """The per-launch lock of one game."""
    return locks_dir() / f"game-{validate_pid(pid)}.lock"


def _take_component_locks(
    stack: contextlib.ExitStack, timeout_s: float,
) -> ActiveComponents | None:
    """Read ``active.json`` under a shared ``install.lock`` and lock both
    components shared on ``stack``. ``install.lock`` is released on return."""
    with flock_path(install_lock_path(), shared=True, timeout_s=timeout_s) as ok:
        if not ok:
            raise ComponentsBusyError("install.lock is held by a component install")
        active = read_active()
        if active is None:
            return None
        for lock in (
            backend_lock_path(active.backend_dir.name),
            runtime_lock_path(active.runtime_dir.name),
        ):
            if not stack.enter_context(
                flock_path(lock, shared=True, timeout_s=timeout_s),
            ):
                raise ComponentsBusyError(f"cannot take {lock.name}")
    return active


@contextlib.contextmanager
def reading_components(
    *, timeout_s: float = SHARED_LOCK_WAIT_S,
) -> Iterator[ActiveComponents | None]:
    """Hold the installed components for the ``with`` body.

    Yields the components with both session locks held shared until exit,
    or None when nothing is installed. Raises :class:`ComponentsBusyError` when
    ``install.lock`` stays held for ``timeout_s`` (an install is promoting).
    """
    root = data_root()
    if not root.is_dir():
        yield None
        return
    ensure_private_dir(root)
    ensure_private_dir(locks_dir())
    with contextlib.ExitStack() as stack:
        yield _take_component_locks(stack, timeout_s)


def components_in_use() -> bool:
    """Whether any session or game lock is held right now.

    True means a game is running. The plugin then defers ``library`` and
    ``profile``, which refresh the same tokens the running game uses.
    """
    folder = locks_dir()
    if not folder.is_dir():
        return False
    return any(not try_exclusive(lock) for lock in sorted(folder.glob("*.lock")))


def _make_writable_and_retry(func: Callable[..., Any], target: str, exc: Any) -> None:
    """rmtree's error hook: give the parent folder u+w once, then retry.

    An archive can hold a 0555 folder, and its entries cannot be removed
    until it is writable again. Any other failure is raised as it was.
    ``exc`` is the exception (``onexc``) or the ``sys.exc_info()`` tuple
    (``onerror``, before Python 3.12).
    """
    if func not in (os.rmdir, os.unlink, os.remove):
        raise exc if isinstance(exc, BaseException) else exc[1]
    parent = os.path.dirname(target)
    os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
    func(target)


def _remove_tree(path: Path) -> None:
    """Delete ``path`` without following a symlink. Raises OSError."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    elif sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def _remove_best_effort(path: Path) -> bool:
    """Delete ``path``. A failure is logged with the path and returns False.

    GC and the staging sweep are cleanup. They run after the new components
    are already active, so they must never fail the install.
    """
    try:
        _remove_tree(path)
    except OSError as err:
        logger.warning("[xbox_pc.locks] could not delete %s, kept for the next GC: %s", path, err)
        return False
    return True


def _gc_one(path: Path, lock: Path) -> bool:
    """Delete one unreferenced component folder if no session holds it."""
    with flock_path(lock, timeout_s=0) as free:
        if not free:
            logger.info("[xbox_pc.locks] keeping %s: a session holds it", path.name)
            return False
        if not _remove_best_effort(path):
            return False
    lock.unlink(missing_ok=True)
    return True


def gc_unreferenced(active: ActiveComponents) -> list[str]:
    """Delete backend and runtime folders that ``active`` does not name.

    Call it with ``install.lock`` held exclusively, so no reader can take a
    new session lock meanwhile. A folder whose lock is held by a running
    session is kept for the next GC. Returns the deleted folder names as
    ``backend/<dir>`` and ``runtime/<dir>``.
    """
    removed: list[str] = []
    keep = {active.backend_dir, active.runtime_dir}
    for parent, lock_for in (
        (backend_root(), backend_lock_path),
        (runtime_root(), runtime_lock_path),
    ):
        if not parent.is_dir():
            continue
        for path in sorted(parent.iterdir()):
            if path in keep:
                continue
            if _gc_one(path, lock_for(path.name)):
                removed.append(f"{parent.name}/{path.name}")
    return removed


def sweep_staging(keep: Path | None = None) -> list[str]:
    """Delete stale ``.staging-*`` folders except ``keep``.

    Call it with ``install.lock`` held exclusively. A crash or a plugin
    unload mid-extract leaves one behind (about 1.5 GB). Returns the names
    deleted. A folder that cannot be deleted is logged and kept.
    """
    root = data_root()
    if not root.is_dir():
        return []
    removed: list[str] = []
    for path in sorted(root.glob(STAGING_PREFIX + "*")):
        if keep is not None and path == keep:
            continue
        if _remove_best_effort(path):
            removed.append(path.name)
    return removed


@contextlib.contextmanager
def game_lock(pid: str) -> Iterator[bool]:
    """Try to hold ``game-<PID>.lock`` exclusively. Yields whether it holds.

    One try, no wait: a held lock means that game is running (or another
    update or uninstall has it), and the caller refuses.
    """
    lock = game_lock_path(pid)
    ensure_private_dir(data_root())
    ensure_private_dir(locks_dir())
    with flock_path(lock, timeout_s=0) as held:
        yield held
