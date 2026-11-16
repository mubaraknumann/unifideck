"""Keep an EA game's machine fingerprint the same on every launch.

EA counts every new machine fingerprint as another PC and refuses the game
after a few ("Too many computers", ``CG_LIMIT_EXCEEDED``). ``maxima-cli`` never
sends a fingerprint other than a game's first (``unifideck-licence-stability``),
but the game computes its own as well, from what Wine reports. Every input is
fixed hardware except one: Wine reports the Windows install date as the
modification time of the prefix's ``drive_c``. Anything written at the top of
``drive_c``, or a recreated prefix, moves it, and older activation versions
hash it. So its first value is kept in the game's install record and put back
before each launch.

Standard library only: the launcher runs on the system Python.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
from pathlib import Path

from .records import read_record, write_record

logger = logging.getLogger(__name__)


def drive_c(prefix: Path) -> Path:
    """The prefix's ``drive_c``: under ``pfx`` for a Proton prefix."""
    nested = prefix / "pfx" / "drive_c"
    return nested if nested.is_dir() else prefix / "drive_c"


def pin_install_date(game_id: str, prefix: Path) -> None:
    """Record the install date on the first launch; restore it on later ones."""
    folder = drive_c(prefix)
    record = read_record(game_id)
    if record is None or not folder.is_dir():
        return
    try:
        stat = folder.stat()
    except OSError:
        return
    if not record.drive_c_mtime_ns:
        with contextlib.suppress(OSError):
            write_record(dataclasses.replace(record, drive_c_mtime_ns=str(stat.st_mtime_ns)))
        return
    first = int(record.drive_c_mtime_ns)
    if stat.st_mtime_ns == first:
        return
    try:
        os.utime(folder, ns=(stat.st_atime_ns, first))
        logger.info("[EA] restored %s's install date so its machine fingerprint stays the same", game_id)
    except OSError as e:
        logger.warning(
            "[EA] could not restore %s's install date at %s (%s): the game may see a new machine fingerprint "
            "and EA may count another PC", game_id, folder, e,
        )
