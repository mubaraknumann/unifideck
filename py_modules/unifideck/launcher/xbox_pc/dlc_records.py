"""launcher/xbox_pc/dlc_records.py — the DLC part of an Xbox PC install record.

An install record (``install_records``) may carry a ``dlc`` map, one entry
per add-on the user chose or declined::

    "dlc": {"9N77PDNB7CWZ": {"state": "selected" | "partial" | "installed"
                                       | "failed" | "declined",
                              "contentId": "...", "title": "...",
                              "sizeBytes": 0, "updatedAt": 0}}

Each DLC is streamed into ``<installRoot>/dlc/<PID>``, beside ``game/``, so
an uninstall of the root removes it and a repair of the game never touches
it.

Rules both processes rely on:

- **Pending** (``selected``, ``partial``, ``failed``): the user wants it and
  it is not installed. The game must not start while any DLC is pending:
  the game page turns Play into Update, and the launcher refuses a start
  that bypassed it.
- Only ``installed`` DLC whose folder exists is handed to the game
  (``XODUS_CONTENT_ROOTS``); a partial download never is.
- ``declined`` is remembered so the DLC is not offered as new again.

A record without ``dlc`` has no DLC. An entry with an unknown state or a
bad product id is ignored, never guessed at.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .layout import PID_RE

DLC_KEY = "dlc"
DLC_STATES = frozenset({"selected", "partial", "installed", "failed", "declined"})
PENDING_STATES = frozenset({"selected", "partial", "failed"})
DLC_DIR_NAME = "dlc"


def dlc_dir_for(install_root: Path, pid: str) -> Path:
    """``<installRoot>/dlc/<PID>``. Raises ``ValueError`` for a bad id."""
    if not isinstance(pid, str) or not PID_RE.match(pid):
        raise ValueError(f"not a product id: {pid!r}")
    return Path(install_root) / DLC_DIR_NAME / pid


def dlc_entries(record: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The record's valid DLC entries by product id (copies)."""
    raw = (record or {}).get(DLC_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        pid: dict(entry) for pid, entry in raw.items()
        if isinstance(pid, str) and PID_RE.match(pid)
        and isinstance(entry, dict) and entry.get("state") in DLC_STATES
    }


def pending_dlc(record: dict[str, Any] | None) -> list[str]:
    """Product ids the user wants and that are not installed, sorted."""
    return sorted(pid for pid, e in dlc_entries(record).items() if e["state"] in PENDING_STATES)


def pending_titles(record: dict[str, Any] | None) -> list[str]:
    """Titles of the pending DLC (the product id when a title is missing)."""
    entries = dlc_entries(record)
    return [str(entries[pid].get("title") or pid) for pid in pending_dlc(record)]


def installed_dlc_roots(record: dict[str, Any] | None) -> list[Path]:
    """Folders of the installed DLC that exist on disk, sorted by product id."""
    root = Path(str((record or {}).get("installRoot") or ""))
    if not str(root) or str(root) == ".":
        return []
    found: list[Path] = []
    for pid, entry in sorted(dlc_entries(record).items()):
        if entry["state"] != "installed":
            continue
        folder = dlc_dir_for(root, pid)
        if folder.is_dir():
            found.append(folder)
    return found


def launch_dlc(record: dict[str, Any] | None) -> tuple[list[str], tuple[Path, ...]]:
    """``(pending DLC, installed DLC folders)``: what a launch refuses on and hands over."""
    return pending_dlc(record), tuple(installed_dlc_roots(record))


def with_dlc_state(
    record: dict[str, Any], pid: str, state: str, **fields: Any,
) -> dict[str, Any]:
    """A copy of ``record`` whose DLC ``pid`` is in ``state`` (other fields kept).

    Raises ``ValueError`` for an unknown state or a bad product id.
    """
    if state not in DLC_STATES:
        raise ValueError(f"unknown DLC state: {state!r}")
    if not isinstance(pid, str) or not PID_RE.match(pid):
        raise ValueError(f"not a product id: {pid!r}")
    entries = dlc_entries(record)
    entry = dict(entries.get(pid) or {})
    entry.update(fields)
    entry.update(state=state, updatedAt=time.time())
    entries[pid] = entry
    return {**record, DLC_KEY: entries}
