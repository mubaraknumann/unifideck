"""services/cloud_save/safety.py — shared save-data guardrails.

Protects against the catastrophic failure mode where an *upload* (to the
store cloud via gogdl/legendary) propagates a LOCAL save loss and **wipes
the cloud copy**. This happens whenever the local save dir is missing its
saves but still exists — e.g. a reset/recreated Wine prefix, a fresh
device, or a failed/partial ``sync_down`` — because the store sync tools
reconcile deletions (local-missing ⇒ delete-from-cloud).

Shared by BOTH the GOG and Epic strategies so the two can't drift. Layers:

  1. :func:`has_save_data` — there must be real save files (inside a
     subdirectory like ``gamesaves/``), not just top-level settings.
  2. :func:`lost_saves_vs_manifest` — the last successful sync recorded a
     manifest (``.unifideck_sync.json``, ``{rel_path: mtime}``); if files
     it tracked are now MISSING, the local copy regressed and must NOT be
     pushed.
  3. :func:`snapshot_backup` — before any sync, snapshot the local saves
     to a rotating versioned backup so nothing is ever unrecoverable.

A blocked upload raises :class:`SaveConflictError`, which the
CloudSaveService turns into a user-facing conflict (CloudSaveConflictModal
via the existing ``retry-sync`` flow) instead of silently destroying data.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .constants import MANIFEST_FILE

logger = logging.getLogger(__name__)

# Manifest written at the root of each synced save dir. The comment here
# used to claim this and ``manifest.py`` "share one source of truth" while
# both held their own literal; they now actually do.
_MANIFEST_NAME = MANIFEST_FILE


def _backups_root() -> Path:
    """Where pre-sync save backups live.

    Resolved per call, not at import. As a module constant this expanded the
    developer's real ``$HOME`` before pytest's autouse fixture could redirect
    it, so the suite wrote fixture saves (``epic/game123``, ``gog/gog123``)
    into the live backup tree. Same trap, same fix as
    ``wrapper_session.prefix_index_path``.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "save_backups"

_KEEP_BACKUPS = 5

# File suffixes that are settings/config, NOT real save data. A save dir
# that contains only these (e.g. a reset Wine prefix where the game
# re-wrote its settings on launch but the actual saves are gone) must not
# be treated as "has saves" — uploading it would wipe the cloud. ``.bak``
# is treated as config too (settings backups like ``profile.settings.bak``).
_CONFIG_SUFFIXES = frozenset(
    {".settings", ".ini", ".cfg", ".config", ".bak", ".tmp", ".log"},
)


class SaveConflictError(Exception):
    """Raised to BLOCK a destructive sync that would lose saves.

    Carries the machine ``reason`` plus a local :func:`snapshot` so the
    service can surface the cloud-save conflict modal with real numbers.

    ``hard`` distinguishes the two cases:

      * ``hard=True``  — the local copy has **no real save data** (empty /
        reset prefix). Uploading it could only ever WIPE the cloud, so this
        is never a valid choice: the service refuses it outright (error,
        no "keep local" option). There is no code path — not even a forced
        retry — that can push an empty save set.
      * ``hard=False`` — a genuine divergence/regression where the local
        copy still has saves. The service surfaces the conflict modal so
        the user picks local vs cloud.
    """

    def __init__(
        self, reason: str, local: dict[str, Any], *, store: str, game_id: str,
        hard: bool = False,
    ) -> None:
        super().__init__(f"{reason} ({store}:{game_id})")
        self.reason = reason
        self.local = local
        self.store = store
        self.game_id = game_id
        self.hard = hard


def _iter_save_files(directory: Path) -> Iterator[Path]:
    """Yield regular save files under ``directory`` (excluding the manifest)."""
    if not directory.exists():
        return
    for entry in directory.rglob("*"):
        if entry.is_file() and entry.name != _MANIFEST_NAME:
            yield entry


def _is_real_save_file(entry: Path) -> bool:
    """True if ``entry`` looks like actual save data, not config/manifest."""
    if entry.name == _MANIFEST_NAME:
        return False
    name = entry.name.lower()
    if name.endswith(".bak"):  # e.g. ``profile.settings.bak``
        return False
    return entry.suffix.lower() not in _CONFIG_SUFFIXES


def has_save_data(directory: str | Path) -> bool:
    """True if the dir holds at least one *real* save file (not just config).

    A save dir that contains only settings/config files (``*.settings``,
    ``*.ini``, ``*.bak`` …) — the state a freshly reset Wine prefix has
    after the game re-creates its config but before saves are restored — is
    NOT treated as having saves. Pushing that state would delete the real
    saves from the cloud, so callers must refuse to upload it.
    """
    root = Path(directory)
    try:
        return any(
            _is_real_save_file(p) for p in root.rglob("*") if p.is_file()
        )
    except OSError:
        return False


def _manifest_files(directory: Path) -> set[str]:
    """Return the set of rel paths recorded in the last-sync manifest."""
    manifest_path = directory / _MANIFEST_NAME
    if not manifest_path.is_file():
        return set()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return set(data) if isinstance(data, dict) else set()


def lost_saves_vs_manifest(directory: str | Path) -> bool:
    """True if files recorded in the last-sync manifest are now MISSING.

    The regression signal: the local copy used to contain files (per the
    manifest) that have since vanished — a reset/partial prefix. Pushing
    this state would delete those files from the cloud. With no manifest
    baseline we can't tell, so return False and let the other guards apply.
    """
    root = Path(directory)
    recorded = _manifest_files(root)
    if not recorded:
        return False
    present = {str(p.relative_to(root)) for p in _iter_save_files(root)}
    return bool(recorded - present)


def snapshot(directory: str | Path) -> dict[str, Any]:
    """Return ``{timestamp, file_count, total_bytes}`` for the save dir.

    Shape matches what ``CloudSaveConflictModal`` renders.
    """
    root = Path(directory)
    file_count = 0
    total_bytes = 0
    newest = 0.0
    for entry in _iter_save_files(root):
        try:
            st = entry.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += st.st_size
        newest = max(newest, st.st_mtime)
    return {
        "timestamp": newest,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _rotate_backups(dest_root: Path) -> None:
    """Keep only the newest ``_KEEP_BACKUPS`` snapshot dirs."""
    try:
        snaps = sorted(
            (p for p in dest_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
        )
        for old in snaps[:-_KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


def snapshot_backup(
    directory: str | Path, store: str, game_id: str,
    *, now: float | None = None,
) -> Path | None:
    """Snapshot the save dir to a rotating versioned backup. Best-effort.

    Backups live at
    ``~/.local/share/unifideck/save_backups/<store>/<game_id>/<unix_ts>/``;
    the newest ``_KEEP_BACKUPS`` are retained. Returns the backup path, or
    None when there was nothing to back up or it failed. Never raises — a
    failed backup must not break a launch.
    """
    src = Path(directory)
    if not has_save_data(src):
        return None
    dest_root = _backups_root() / store / game_id
    stamp = str(int(now if now is not None else time.time()))
    dest = dest_root / stamp
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        _rotate_backups(dest_root)
        logger.info("[CloudSafety] Backed up %s/%s saves -> %s", store, game_id, dest)
        return dest
    except Exception as e:
        logger.warning(
            "[CloudSafety] Backup failed for %s/%s: %s", store, game_id, e,
        )
        return None


def latest_backup_snapshot(store: str, game_id: str) -> dict[str, Any]:
    """``snapshot`` of the most recent versioned backup, or zeros if none.

    Lets the conflict modal show a cheap, local approximation of the
    cloud-side copy without a full store download.
    """
    dest_root = _backups_root() / store / game_id
    try:
        snaps = sorted(
            (p for p in dest_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
        )
    except OSError:
        snaps = []
    if not snaps:
        return {"timestamp": 0.0, "file_count": 0, "total_bytes": 0}
    return snapshot(snaps[-1])


def assert_has_saves(
    directory: str | Path, store: str, game_id: str,
) -> None:
    """Final, unconditional gate: raise unless there's real save data.

    Call this immediately before invoking the store's upload CLI. It makes
    pushing an empty / settings-only save set impossible — no path (normal
    or forced) can ever wipe the cloud. Always a HARD error.
    """
    if not has_save_data(directory):
        raise SaveConflictError(
            "no_local_save_data", snapshot(directory),
            store=store, game_id=game_id, hard=True,
        )


def guard_before_upload(
    directory: str | Path, store: str, game_id: str,
) -> None:
    """Back up, then verify it's SAFE to push local → cloud, else raise.

    Raises :class:`SaveConflictError` when the local copy has no real save
    data, or regressed vs the last-sync manifest — both of which would make
    the store's reconciling sync DELETE saves from the cloud. The caller
    must NOT run the destructive upload when this raises; the service turns
    the error into a user-facing conflict instead.
    """
    snapshot_backup(directory, store, game_id)
    # HARD: an empty / settings-only dir can only WIPE the cloud — never a
    # valid upload, refused unconditionally.
    assert_has_saves(directory, store, game_id)
    if lost_saves_vs_manifest(directory):
        raise SaveConflictError(
            "local_saves_regressed", snapshot(directory),
            store=store, game_id=game_id, hard=False,
        )
