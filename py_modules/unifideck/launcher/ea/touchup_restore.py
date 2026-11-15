"""Put back the files an earlier Touchup moved, so Touchup can run again.

Touchup *moves* the chosen language's files from ``__Installer/LOC/<locale>/``
into the game folder. A second run, needed whenever the game gets a new prefix
(the prefix's registry and redistributables are Touchup's job too), looks for
those files where they were and fails with "Missing source file", exit 2.

Touchup logs every move in ``__Installer/InstallLog.txt`` (``Moving: <src> to
<dst>``), so before a run each moved file whose source is gone is moved back
from where it landed, and Touchup's run moves it in again. Moving costs no space
(a copy of one language can be close to a gigabyte). A hard link does not work:
Touchup's move onto a link of the same file fails. Should Touchup fail after
this, the game is not launched and the next launch runs it again. A build
Touchup never ran on has no log and nothing happens.

Touchup names the files it moves as its log does, lower-cased. When it moves
a file whose name is not in the game folder yet (always, after a restore), the
lower-cased name stays: ``syberia2.exe`` where the install record launches
``Syberia2.exe``, so the launch found no exe. Wine does not care about case;
Linux paths do. :func:`match_ledger_case` puts every file back under the name
the download ledger (``.maxima-files``) recorded.

Standard library only: the launcher runs on the system Python.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_LOG = Path("__Installer") / "InstallLog.txt"
#: The download ledger ``maxima-cli install`` keeps (``unifideck-install``): every file, by its real name.
LEDGER = ".maxima-files"
_MOVE_RE = re.compile(r"Moving:\s+(.+?)\s+to\s+\S.*$")
#: The part of a move's source below the build: ``LOC\<locale>\<path in the game folder>``.
_LOC_RE = re.compile(r"\\__installer\\loc\\([A-Za-z]{2}_[A-Za-z]{2})\\(.+)$", re.IGNORECASE)


def _moves(install: Path) -> list[tuple[str, str]]:
    """``(locale, relative path)`` for each file Touchup logged as moved."""
    try:
        text = (install / INSTALL_LOG).read_bytes().decode("utf-16", errors="replace")
    except (OSError, UnicodeError):
        return []
    moves = []
    for line in text.splitlines():
        move = _MOVE_RE.search(line)
        loc = _LOC_RE.search(move.group(1)) if move else None
        if loc is not None:
            moves.append((loc.group(1), loc.group(2).replace("\\", "/")))
    return moves


def _resolve(base: Path, relative: str) -> Path:
    """``base/relative``, each part matched without case (the log is lower-cased, the disk is not)."""
    path = base
    for part in relative.split("/"):
        exact = path / part
        if not exact.exists() and path.is_dir():
            exact = next((child for child in path.iterdir() if child.name.lower() == part.lower()), exact)
        path = exact
    return path


def _put_back(landed: Path, source: Path) -> bool:
    """Move ``landed`` to ``source``; True when ``source`` now holds it."""
    present = _resolve(source.parent, source.name)
    if present.exists():
        if not os.path.samefile(present, landed):
            return False  # the source is there already
        landed.unlink()  # a link left by an earlier restore: drop the game folder's name
        return True
    source.parent.mkdir(parents=True, exist_ok=True)
    landed.rename(source)
    return True


def restore_moved_files(install: Path) -> int:
    """Move back every file an earlier Touchup moved out of ``LOC``; how many."""
    restored = 0
    for locale, relative in _moves(install):
        landed = _resolve(install, relative)
        if not landed.is_file():
            continue
        # Named after the file on disk, so a folder created here keeps its real case.
        source = _resolve(install / "__Installer" / "LOC", locale) / landed.relative_to(install)
        try:
            restored += _put_back(landed, source)
        except OSError as e:
            logger.warning("[EA] could not put back %s for Touchup: %s", source, e)
    if restored:
        logger.info("[EA] put back %d file(s) an earlier Touchup moved, so it can run again", restored)
    return restored


def match_ledger_case(install: Path) -> int:
    """Rename files Touchup left lower-cased back to their ledger names; how many."""
    try:
        files = json.loads((install / LEDGER).read_text()).get("files") or {}
    except (OSError, ValueError, AttributeError):
        return 0
    renamed = 0
    for relative in files:
        wanted = install / relative
        if wanted.exists() or not wanted.parent.is_dir():
            continue
        found = next((c for c in wanted.parent.iterdir() if c.name.lower() == wanted.name.lower()), None)
        if found is None or not found.is_file():
            continue
        try:
            found.rename(wanted)
        except OSError as e:
            logger.warning("[EA] could not rename %s back to %s: %s", found.name, wanted.name, e)
            continue
        renamed += 1
    if renamed:
        logger.info("[EA] gave %d file(s) Touchup lower-cased their real names back", renamed)
    return renamed
