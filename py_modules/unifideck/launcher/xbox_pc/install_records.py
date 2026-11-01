"""launcher/xbox_pc/install_records.py — one JSON record per installed game.

``installed/<PID>.json`` is written by the game installer and read by the
library (to list installed games) and the launcher (to run them)::

    {"schema": 1, "productId": "9PK087LNGJC5",
     "state": "partial" | "installed" | "updating",
     "market": "IN", "contentId": "...",
     "installRoot": "<base>/Xbox/<PID>", "gameDir": "<installRoot>/game",
     "exe": "<absolute>", "exeRelative": "...", "version": "...",
     "titleId": "...", "startedAt": 0, "installedAt": 0}

A record that fails to parse is reported, never deleted: the broken state
is the evidence (CLAUDE.md rule 2).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .layout import (
    PID_RE,
    data_root,
    ensure_private_dir,
    installed_dir,
    read_json_dict,
    validate_pid,
    write_private_json,
)

RECORD_SCHEMA = 1
RECORD_STATES = frozenset({"partial", "installed", "updating"})


def record_path(pid: str) -> Path:
    """``installed/<PID>.json``."""
    return installed_dir() / f"{validate_pid(pid)}.json"


def _valid_record(data: dict[str, Any] | None, pid: str) -> dict[str, Any] | None:
    """``data`` when it is a record of ``pid`` in a known state, else None."""
    if data is None:
        return None
    if data.get("schema") != RECORD_SCHEMA or data.get("productId") != pid:
        return None
    if data.get("state") not in RECORD_STATES:
        return None
    return data


def read_install_record(pid: str) -> dict[str, Any] | None:
    """The record of ``pid``, or None when absent, unreadable or invalid.

    An id that is not a product id gives None rather than an error, so a
    caller can pass any ``store_game_id``.
    """
    if not isinstance(pid, str) or not PID_RE.match(pid):
        return None
    return _valid_record(read_json_dict(record_path(pid)), pid)


def list_install_records() -> tuple[list[dict[str, Any]], list[Path]]:
    """``(valid records, paths of record files that did not parse)``.

    Every ``*.json`` in ``installed/`` counts, ``partial`` ones included.
    A file whose name is not ``<PID>.json`` is listed as unreadable. Atomic
    write temp files (``.*.tmp``) are skipped.
    """
    folder = installed_dir()
    if not folder.is_dir():
        return [], []
    records: list[dict[str, Any]] = []
    unreadable: list[Path] = []
    for path in sorted(folder.glob("*.json")):
        pid = path.stem
        record = None
        if PID_RE.match(pid):
            record = _valid_record(read_json_dict(path), pid)
        if record is None:
            unreadable.append(path)
        else:
            records.append(record)
    return records, unreadable


def write_install_record(record: dict[str, Any]) -> Path:
    """Write ``record`` atomically (0600) and return its path.

    Raises ``ValueError`` when the record has the wrong schema, product id
    or state, so a bad record is never written.
    """
    pid = validate_pid(str(record.get("productId")))
    if _valid_record(record, pid) is None:
        raise ValueError(f"invalid Xbox PC install record for {pid}")
    ensure_private_dir(data_root())
    ensure_private_dir(installed_dir())
    path = record_path(pid)
    write_private_json(path, record)
    return path


def remove_install_record(pid: str) -> None:
    """Delete the record of ``pid``. A missing one is not an error."""
    record_path(pid).unlink(missing_ok=True)
