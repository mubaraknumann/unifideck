"""services/cloud_save/sync_record.py — what each game looked like at its last sync.

The cloud-save status line has to say whether local and cloud saves agree.
Comparing timestamps cannot answer that: a store reports when the cloud copy
was *uploaded*, the save folder says when the game *wrote* the file, and the
two differ by minutes even right after a clean push. A store with no cloud
query at all was worse: the line compared local saves with the plugin's own
backup mirror, which only changes when a sync runs, so every play session read
as "out of sync".

So after every successful pull or push the base strategy records two things:
a fingerprint of the local save folder and the cloud's timestamp. The verdict
is then a three-way compare against that record:

* local fingerprint unchanged, cloud timestamp unchanged → ``in_sync``
* local changed only → ``local_newer``
* cloud newer only → ``cloud_newer``
* both → ``conflict``

A store that can compare file by file (EA, through ``maxima-cli``) reports its
own verdict instead. The record lives in the plugin's data folder, never in a
save folder, so no store can upload it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from unifideck.core.save_sync_state import (
    CLOUD_NEWER,
    CONFLICT,
    IN_SYNC,
    LOCAL_NEWER,
    VERDICTS,
)

from .constants import MANIFEST_FILE

logger = logging.getLogger(__name__)

RECORDS_FILE = "cloud_sync_records.json"

__all__ = [
    "CLOUD_NEWER", "CONFLICT", "IN_SYNC", "LOCAL_NEWER", "RECORDS_FILE", "VERDICTS",
    "SyncRecords", "fingerprint", "verdict",
]

#: A cloud timestamp this much newer than the recorded one is a new upload.
_CLOUD_TS_TOLERANCE_S = 2.0


def fingerprint(directory: str | Path) -> str | None:
    """A hash of every file's path, size and mtime under ``directory``.

    None when the folder does not exist. The plugin's own sync manifest is
    left out, since writing it is not a change to the saves.
    """
    root = Path(directory)
    if not root.is_dir():
        return None
    entries: list[str] = []
    for path in root.rglob("*"):
        if path.name == MANIFEST_FILE:
            continue
        try:
            if not path.is_file():
                continue
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        entries.append(f"{rel}\0{st.st_size}\0{int(st.st_mtime)}")
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


class SyncRecords:
    """``{"<store>:<game_id>": {"local": fingerprint, "cloud_ts": float}}`` on disk."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._load().get(key)
        return entry if isinstance(entry, dict) else None

    def put(self, key: str, local: str | None, cloud_ts: float | None) -> None:
        data = self._load()
        data[key] = {"local": local, "cloud_ts": cloud_ts}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("[CloudSync] could not record the sync for %s: %s", key, e)
            with contextlib.suppress(OSError):
                tmp.unlink()


def verdict(
    record: dict[str, Any] | None, local: str | None, cloud_ts: float | None,
) -> str | None:
    """The three-way verdict, or None when there is nothing to compare with.

    ``cloud_ts`` None means the store cannot report one; only the local side
    is judged then.
    """
    if record is None or local is None:
        return None
    local_changed = record.get("local") != local
    recorded_ts = record.get("cloud_ts")
    cloud_changed = (
        cloud_ts is not None
        and recorded_ts is not None
        and cloud_ts > float(recorded_ts) + _CLOUD_TS_TOLERANCE_S
    )
    if local_changed and cloud_changed:
        return CONFLICT
    if local_changed:
        return LOCAL_NEWER
    if cloud_changed:
        return CLOUD_NEWER
    return IN_SYNC
