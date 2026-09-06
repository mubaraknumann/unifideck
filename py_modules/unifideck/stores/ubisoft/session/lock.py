"""Cross-process lock serialising UPC session moves.

Two processes rewrite the same credential files. The backend fans a
captured session out across every Ubisoft prefix
(``propagate_all_to_all``); the out-of-process launcher seeds one prefix
before UPC starts and captures the rotated token after it exits. Nothing
coordinated them, so a fan-out could interleave with a seed and leave a
prefix holding half of one session and half of another.

``fcntl.flock`` is the right primitive here: it is stdlib (the launcher
runs under the *system* python and can import nothing else at load
time), it is released automatically if a process is SIGKILLed with the
game, and it works across the backend/launcher boundary. Precedent:
``launcher/proton/compat/gog_setup/redist.py``.

The lock is advisory and **best-effort by design**. A launch must never
fail, or even stall noticeably, because a lock file could not be taken —
so acquisition is bounded and the caller proceeds unlocked on timeout,
which is exactly the behaviour that shipped before this module existed.
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

LOCK_FILENAME = "ubisoft-session.lock"

#: A fan-out across every prefix copies a few MB of cache; a seed copies
#: less. Ten seconds is far longer than either and still short enough that
#: a stuck holder cannot visibly delay a game launch.
_ACQUIRE_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.1


@contextlib.contextmanager
def session_lock(data_dir: str) -> Iterator[bool]:
    """Hold the UPC session lock for the body; yield whether it was taken.

    Yields ``True`` when the lock is held and ``False`` when it could not
    be acquired within the timeout. Callers run their body either way —
    the yielded flag is for logging, not for skipping work.
    """
    lock_path = Path(data_dir).expanduser() / LOCK_FILENAME
    fd: int | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        logger.debug("[UbisoftSession] session lock unavailable: %s", e)
        yield False
        return
    acquired = _acquire(fd, lock_path)
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _acquire(fd: int, lock_path: Path) -> bool:
    """Poll for the exclusive lock until the timeout; True if taken."""
    deadline = time.monotonic() + _ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "[UbisoftSession] could not take %s within %.0fs — "
                    "proceeding unserialised",
                    lock_path.name,
                    _ACQUIRE_TIMEOUT_SECONDS,
                )
                return False
            time.sleep(_POLL_SECONDS)
