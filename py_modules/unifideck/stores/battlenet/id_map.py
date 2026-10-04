"""Persistent per-game state for the Battle.net store.

py_modules/unifideck/stores/battlenet/id_map.py

Records, per game uid: the family code used to launch it, where its prefix
actually lives, where the game installed to, and whether a launch has ever
been observed to work.

Two of those exist because of measured failure modes:

* **``prefix_path`` is recorded, never reconstructed.** A Ubisoft incident
  (``prefix_setup.py``) had a rebuilt path stamp a marker into a directory
  no launch ever opened, producing a permanent reset loop. It also lets a
  game live on an SD card without the resolver guessing.
* **``launch_ok_at`` / ``last_launch_family``** exist because an obsolete
  family code **fails silently**: Blizzard renamed Diablo IV from ``D4`` to
  ``Fen`` and the client accepted the dead code, launching nothing, with no
  error. A family proven to work once is never second-guessed, and one that
  has never worked is a candidate for a catalog refresh.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

ID_MAP_FILENAME = "battlenet_id_map.json"


@dataclass(frozen=True, slots=True)
class GameRecord:
    """What we know about one Battle.net game locally."""

    uid: str
    family: str | None = None
    prefix_path: str | None = None
    install_path: str | None = None
    exe_path: str | None = None
    # True when ``family`` selects a version the client will not auto-launch
    # (anything but the program's own retail product), so the user presses
    # Play in the client. Measured: 'launch WoWC' selects Classic Era and
    # stops there, while 'launch W3' goes straight to LaunchBinary.
    client_selects: bool = False
    last_launch_family: str | None = None
    launch_ok_at: float | None = None
    total_bytes: int | None = None
    client_build: str | None = None

    @property
    def launch_proven(self) -> bool:
        """True when this family has actually started the game before."""
        return self.launch_ok_at is not None


class BattlenetIdMap:
    """A small JSON store keyed by game uid. Never raises on IO."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._records: dict[str, GameRecord] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("[Battlenet] cannot read %s: %s", self._path, exc)
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("[Battlenet] %s is corrupt, ignoring: %s", self._path, exc)
            return
        if not isinstance(data, dict):
            return
        for uid, entry in data.items():
            if isinstance(uid, str) and isinstance(entry, dict):
                self._records[uid] = self._record_from(uid, entry)

    @staticmethod
    def _record_from(uid: str, entry: dict[str, object]) -> GameRecord:
        fields = {f for f in GameRecord.__slots__ if f != "uid"}
        kwargs = {k: v for k, v in entry.items() if k in fields}
        return GameRecord(uid=uid, **kwargs)  # type: ignore[arg-type]

    def _save(self) -> None:
        """Atomic write — a torn id map would lose every prefix path."""
        payload = {
            uid: {k: v for k, v in asdict(rec).items() if k != "uid" and v is not None}
            for uid, rec in sorted(self._records.items())
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            Path(tmp).replace(self._path)
        except OSError as exc:
            logger.warning("[Battlenet] cannot write %s: %s", self._path, exc)

    # -- reads -------------------------------------------------------------

    def get(self, uid: str) -> GameRecord | None:
        return self._records.get(uid)

    def all_records(self) -> dict[str, GameRecord]:
        return dict(self._records)

    def resolve_family(self, uid: str) -> str | None:
        record = self._records.get(uid)
        return record.family if record else None

    def resolve_prefix(self, uid: str) -> Path | None:
        """The recorded prefix for a game. Never guesses."""
        record = self._records.get(uid)
        if record is None or not record.prefix_path:
            return None
        return Path(record.prefix_path)

    def all_prefix_paths(self) -> list[Path]:
        """Every prefix we know about — the sweep set for the prefix bridge."""
        return [
            Path(r.prefix_path) for r in self._records.values() if r.prefix_path
        ]

    # -- writes ------------------------------------------------------------

    def merge(self, uid: str, **fields: object) -> GameRecord:
        """Update a record in place, keeping values not being overwritten."""
        record = self._merged(uid, fields)
        self._records[uid] = record
        self._save()
        return record

    def _merged(self, uid: str, fields: dict[str, object]) -> GameRecord:
        """The record ``uid`` would become, without persisting it."""
        current = self._records.get(uid) or GameRecord(uid=uid)
        merged: dict[str, object] = {
            k: v for k, v in asdict(current).items() if k != "uid"
        }
        merged.update({k: v for k, v in fields.items() if v is not None})
        return GameRecord(uid=uid, **merged)  # type: ignore[arg-type]

    def merge_many(self, updates: dict[str, dict[str, object]]) -> int:
        """Merge several records, writing the file at most once.

        A sync refreshes the family code for the whole library at once;
        per-record :meth:`merge` would rewrite the file once per title. The
        file is only written when something actually changed, so a sync that
        learns nothing new does no IO at all.

        Returns the number of records that changed.
        """
        changed = 0
        for uid, fields in updates.items():
            record = self._merged(uid, fields)
            if record != self._records.get(uid):
                self._records[uid] = record
                changed += 1
        if changed:
            self._save()
        return changed

    def mark_launch_ok(self, uid: str, family: str, when: float) -> None:
        """Record that this family actually started the game.

        ``when`` is passed in rather than read from the clock so callers
        stay testable and the module has no time dependency.
        """
        self.merge(uid, family=family, last_launch_family=family, launch_ok_at=when)

    def clear_prefix(self, uid: str) -> None:
        """Forget where a game's prefix was, keeping the rest of the record.

        Used when an abandoned prefix is deleted. :meth:`merge` cannot do
        this — it drops ``None`` values so a partial update never wipes a
        field it was not given — and :meth:`forget` would take the family
        code and launch history with it.
        """
        record = self._records.get(uid)
        if record is None or record.prefix_path is None:
            return
        self._records[uid] = replace(record, prefix_path=None)
        self._save()

    def forget(self, uid: str) -> None:
        """Drop a record — used on uninstall. Does not touch the prefix."""
        if self._records.pop(uid, None) is not None:
            self._save()
