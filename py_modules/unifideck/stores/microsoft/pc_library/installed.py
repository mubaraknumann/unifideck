"""The Xbox PC games installed on this Deck, read from their install records.

The installer writes one record per game, ``xbox_pc/installed/<PID>.json``
(``launcher/xbox_pc/install_records.py``). The library reads them in two
ways, and keeping them apart is what keeps a tile alive:

- **every** record, ``partial`` ones included, so a game whose folder is on
  an unmounted SD card keeps its row (shown as not installed) instead of
  having its shortcut swept;
- the **verified** records, which alone mark a row installed: state
  ``installed`` or ``updating``, the game folder present, an absolute exe
  that exists, and a ``MicrosoftGame.config`` (any case, I-010).

A record file that does not parse is reported, never deleted or renamed:
the broken state is the evidence (CLAUDE.md rule 2).

Blocking file I/O: call :func:`load_installed` through ``asyncio.to_thread``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.launcher.xbox_pc.install_records import list_install_records
from unifideck.launcher.xbox_pc.layout import PID_RE

GAME_CONFIG_NAME = "microsoftgame.config"
#: Record states whose game files are on disk. ``updating`` is a delta over
#: an installed game, so the game is still there.
ON_DISK_STATES = frozenset({"installed", "updating"})


@dataclass(frozen=True)
class InstalledState:
    """What the record folder holds right now.

    ``records`` has every valid record by product id. ``verified`` is the
    subset that passed the disk check. ``unreadable`` lists record files that
    did not parse.
    """

    records: dict[str, dict[str, Any]]
    verified: dict[str, dict[str, Any]]
    unreadable: tuple[Path, ...] = ()

    def unreadable_ids(self) -> list[str]:
        """Product ids of unreadable records, from their file names."""
        return [p.stem for p in self.unreadable if PID_RE.match(p.stem)]

    def floor_records(self) -> dict[str, dict[str, Any]]:
        """Every record that must keep a row, unreadable ones included.

        An unreadable record still names its product in the file name, so
        its row is kept (as not installed) until the file is fixed.
        """
        floor: dict[str, dict[str, Any]] = {pid: {"productId": pid} for pid in self.unreadable_ids()}
        floor.update(self.records)
        return floor


def find_game_config(game_dir: Path) -> Path | None:
    """The game's ``MicrosoftGame.config`` in any letter case, or None."""
    try:
        for entry in game_dir.iterdir():
            if entry.name.lower() == GAME_CONFIG_NAME and entry.is_file():
                return entry
    except OSError:
        return None
    return None


def is_verified(record: dict[str, Any]) -> bool:
    """Whether the record's game is really on disk and can be launched."""
    if record.get("state") not in ON_DISK_STATES:
        return False
    root, game_dir, exe = record.get("installRoot"), record.get("gameDir"), record.get("exe")
    if not all(isinstance(v, str) and v for v in (root, game_dir, exe)):
        return False
    exe_path, game_path = Path(str(exe)), Path(str(game_dir))
    if not exe_path.is_absolute() or not game_path.is_absolute():
        return False
    try:
        if not game_path.is_dir() or not exe_path.is_file():
            return False
    except OSError:
        return False
    return find_game_config(game_path) is not None


def load_installed() -> InstalledState:
    """Read and check every install record. Blocking."""
    records, unreadable = list_install_records()
    by_pid = {str(r["productId"]): r for r in records}
    verified = {pid: r for pid, r in by_pid.items() if is_verified(r)}
    return InstalledState(records=by_pid, verified=verified, unreadable=tuple(unreadable))
