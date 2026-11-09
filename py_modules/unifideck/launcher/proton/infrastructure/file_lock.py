"""launcher/proton/infrastructure/file_lock.py — cross-process file locks.

One home for ``fcntl.flock`` on a path. The GE-Proton installer and the Xbox
PC component store both serialise work across OS processes (the Decky
backend and every launcher), so an in-process lock is never enough.

``fcntl.flock`` has no timeout parameter. A bounded wait is therefore a
``LOCK_NB`` poll loop. Stdlib only: this runs under the launcher's system
Python (3.10 to 3.14).
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

#: How often a bounded wait retries a held lock.
POLL_INTERVAL_S = 0.1


def _acquire(fd: int, flag: int, timeout_s: float | None) -> bool:
    """Take ``flag`` on ``fd``. Blocks when ``timeout_s`` is None."""
    if timeout_s is None:
        fcntl.flock(fd, flag)
        return True
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            fcntl.flock(fd, flag | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
        time.sleep(POLL_INTERVAL_S)


@contextlib.contextmanager
def flock_path(
    path: Path,
    *,
    shared: bool = False,
    timeout_s: float | None = None,
    mode: int = 0o600,
) -> Iterator[bool]:
    """Hold a lock on ``path`` for the ``with`` body. Yields whether it holds.

    ``shared`` takes ``LOCK_SH`` instead of ``LOCK_EX``. ``timeout_s=None``
    blocks until the lock is free. A number polls with ``LOCK_NB`` and yields
    ``False`` when the time runs out. It also yields ``False`` when the lock
    file cannot be opened, so the caller decides whether to go on without it.

    The parent directory is created when missing. A caller that needs it
    private must create it first.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, mode)
    except OSError as e:
        logger.warning("[file_lock] cannot open lock %s: %s", path, e)
        yield False
        return
    try:
        flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        held = _acquire(fd, flag, timeout_s)
        yield held
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def try_exclusive(path: Path) -> bool:
    """Whether nobody holds ``path`` right now. Releases at once.

    A missing lock file means nobody holds it. An unreadable one counts as
    held, because the caller uses this to decide whether deleting something
    is safe.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)
