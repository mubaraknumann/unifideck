"""Recognising a Ubisoft Connect install arriving in the prefix.

py_modules/unifideck/stores/ubisoft/installer/manual_ui_poll.py

The watching *loop* — timeouts, the two give-up watchdogs, completion, progress
ticks — used to live here and now lives once, in
:mod:`unifideck.stores.shared.wrapper_install.watch`, shared with Battle.net and
whatever wrapper store comes next. What is left is the part that is genuinely
Ubisoft's: how you tell that UPC has put a game on disk.

UPC publishes no completion *message*, and the client stays running in a
service-mode background loop afterwards, so for a long time this returned
``None`` and left the shared loop watching the install directory's size hold
steady. That was wrong twice over, and it cost an 18-minute-early completion on
a Splinter Cell install: the queue's single slot was released to the next game
while UPC was still writing, and finalisation recorded a launch executable that
UPC then deleted.

* **UPC does say when it is finished, just not in words.** It stages every
  download under ``<game>/uplay_download/`` and drains that directory when the
  install completes. Emptiness is the signal, and it is read here.
* **The size it was watching was a lie.** UPC pre-allocates every file at its
  full final length the instant it accepts the job, so the *apparent* size of a
  2.4 GB game reaches 2.4 GB within seconds and then never moves. To a
  three-unchanged-reads heuristic that is a finished, perfectly stable
  download. :func:`~unifideck.stores.shared.installed_size.dir_allocated_bytes`
  counts committed blocks instead, so progress tracks what has really landed.

The verdict is three-valued and all three arms are load-bearing. ``False``
while staging holds files is not merely "not yet": it *suppresses* the size
heuristic, which is what stops a mid-download pause from ending the install.
``None`` is reserved for a prefix where UPC has not staged anything yet, and
hands the decision back to the (now honest) size fallback.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from unifideck.stores.shared.installed_size import dir_allocated_bytes
from unifideck.stores.ubisoft.library.detection_helpers import (
    UPC_STAGING_DIR,
    looks_like_game_install,
)

logger = logging.getLogger(__name__)

STORE_ID = "ubisoft"
CLIENT_LABEL = "Ubisoft Connect"

_UPC_GAMES_REL = str(
    Path("drive_c") / "Program Files (x86)" / "Ubisoft"
    / "Ubisoft Game Launcher" / "games",
)


def upc_game_dirs(prefix_path: str) -> tuple[str, str]:
    """Both spellings of UPC's in-prefix ``games/`` directory.

    umu makes ``pfx`` a self-symlink to the prefix, so the same directory is
    reachable by two paths and which one appears depends on how the prefix was
    created. Watching only one of them missed real installs.
    """
    return (
        str(Path(prefix_path) / _UPC_GAMES_REL),
        str(Path(prefix_path) / "pfx" / _UPC_GAMES_REL),
    )


def _listing(path: str) -> set[str]:
    """Entry names in ``path``; empty when it does not exist yet.

    An absent directory must baseline as EMPTY rather than be skipped. On a
    fresh prefix UPC creates ``games/`` only once the install starts, so the
    old "watch it only if it already exists" rule left it unwatched and the
    newly-installed game was never detected — a false ``no_install_detected``
    for an install that worked fine.
    """
    try:
        return {entry.name for entry in Path(path).iterdir()}
    except OSError:
        return set()


def _has_exe_outside_staging(install_dir: str) -> bool:
    """Whether a ``.exe`` exists under ``install_dir`` but outside staging.

    Only ever reached once staging is empty, so this walks a finished tree at
    most once per install rather than on every poll.
    """
    root = Path(install_dir)
    staging = root / UPC_STAGING_DIR
    try:
        return any(
            staging not in exe.parents for exe in root.rglob("*.exe")
        )
    except OSError:
        return False

class UbisoftInstallProbe:
    """Detects a UPC install by diffing directory listings.

    Two locations are watched, in priority order: the ``install_base`` we asked
    UPC to use, then UPC's own per-prefix ``games/`` directories — the fallback
    for when UPC overrides the requested path and drops the game in its default
    folder anyway.
    """

    store = STORE_ID
    client_label = CLIENT_LABEL

    def __init__(self, install_base: str, prefix_path: str) -> None:
        self._install_base = install_base
        self._prefix_path = prefix_path

    def snapshot(self) -> dict[str, set[str]]:
        """Baseline every watched directory."""
        watched = (self._install_base, *upc_game_dirs(self._prefix_path))
        return {path: _listing(path) for path in watched}

    def detect(self, baseline: Any) -> str | None:
        """First new directory that looks like a game install, else ``None``.

        ``install_base`` is checked first so the user's chosen location wins
        when UPC honoured it.
        """
        if not isinstance(baseline, dict):
            return None
        ordered = (self._install_base, *upc_game_dirs(self._prefix_path))
        for path in ordered:
            found = self._new_game_dir(path, baseline.get(path, set()))
            if found:
                return found
        return None

    @staticmethod
    def _new_game_dir(base: str, before: set[str]) -> str | None:
        """A directory under ``base`` that is new since ``before`` and is a game."""
        for name in _listing(base) - before:
            candidate = str(Path(base) / name)
            if Path(candidate).is_dir() and looks_like_game_install(candidate):
                return candidate
        return None

    def measure(self, install_dir: str) -> int:
        """Bytes actually committed so far — never the apparent size.

        UPC pre-allocates the whole game as sparse files up front, so
        apparent size is final within seconds of the download starting and
        tells you nothing about progress.
        """
        return dir_allocated_bytes(install_dir)

    def is_complete(self, install_dir: str) -> bool | None:
        """UPC's staging directory, read as a completion signal.

        ``uplay_download/`` holds the download while it runs and is drained
        when UPC finishes moving the game into place. So:

        * absent — UPC has not staged anything yet. ``None``: no opinion,
          let the size fallback decide.
        * non-empty — actively downloading. ``False``, which also suppresses
          the size heuristic for this poll, so a pause cannot end the install.
        * empty, with a real executable outside it — done.

        The executable check is corroboration, not decoration: at the moment
        of the false completion that prompted this, *every* ``.exe`` in the
        tree was still under ``uplay_download/``. Requiring one outside it
        cannot pass until UPC has moved the game into its final layout.

        Not used: ``uplay_install.state``. It does change on completion — it
        grows and gains a leading record — but it is an undocumented protobuf
        and we would be inferring its layout from a single observed install.
        The staging directory is plain filesystem state that means one thing.
        """
        staging = Path(install_dir) / UPC_STAGING_DIR
        try:
            staged = any(staging.iterdir())
        except OSError:
            return None
        if staged:
            return False
        return True if _has_exe_outside_staging(install_dir) else None
