"""launcher/proton/infrastructure/ge_install_lock.py — one installer at a time.

A GE-Proton install republishes a directory that other processes may be
executing out of, so it has to be serialised across processes rather than
within one. Its own module because ``ge_installer`` is at its size cap and
because this is concurrency, not fetch-and-extract.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from unifideck.launcher.proton.infrastructure.file_lock import flock_path

# Held for the whole of ``ensure_latest_ge``. Deliberately NOT inside
# ``compatibilitytools.d`` — Steam scans that directory and treats every entry
# as a candidate compat tool.
INSTALL_LOCK = Path("~/.local/share/unifideck/ge_install.lock").expanduser()


@contextlib.contextmanager
def install_lock() -> Iterator[bool]:
    """Serialise GE-Proton installs across processes. Yields whether it holds.

    ``ensure_latest_ge`` runs from the Decky backend on every plugin load AND
    from every launcher process (``selector._default_latest_ge``,
    ``ge_fallback._resolve_ge_proton``), which are separate OS processes — so
    an in-process lock would not be enough. ``fcntl.flock`` is stdlib and works
    under the launcher's system python.

    Blocking, not ``LOCK_NB``: the loser wants the winner's result, and its
    re-check under the lock then finds a complete install and returns without
    downloading. Yields ``False`` only when the lock file itself cannot be
    opened, in which case the caller proceeds unserialised rather than losing
    the ability to install Proton at all.
    """
    # ``flock_path`` logs the open failure and yields False for it.
    with flock_path(INSTALL_LOCK, mode=0o644) as held:
        yield held
