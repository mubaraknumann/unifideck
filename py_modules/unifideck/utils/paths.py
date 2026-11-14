"""utils/paths.py — Centralized path resolution for game installations.

Refactor of legacy ``utils/paths.py`` (130 lines). Provides a
single source of truth for where Unifideck looks for installed
games: default install dirs per store, mounted SD cards/USB
drives, and optional user-configured custom paths.

The legacy module hardcoded ``~/.local/share/unifideck/...``
paths and a fixed list of store install directories. This
refactor:

- Reads default install paths from ``stores.<n>.install_dir``
- Reads custom override from ``download.custom_path``
- Reads the SD card mount root from ``paths.sd_card_root``
- Returns a deduplicated list of existing directories

Pure helpers (no I/O):

- ``expand`` : tilde + env-var expansion in one shot
- ``dedupe_paths`` : remove duplicates preserving order

Filesystem helpers:

- ``get_all_game_directories(config)`` : full discovery scan
- ``get_games_map_path(config)`` : the games.map location
- ``ensure_games_map_dir(config)`` : create the parent dir

Reference: Technical Document v1.0 — Section 3.6.1 (games.map),
3.9 (ConfigManager), 5.6 (installation pipeline).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import mounts
from .config_helpers import get_cfg

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

# Default install directories per store, used when no override
# is set in ``stores.<n>.install_dir``. These match the legacy
# paths so existing user installs are still discovered.
DEFAULT_INSTALL_DIRS = {
    "epic": "~/Games/Epic",
    "gog": "~/GOG Games",
    "amazon": "~/Games/Amazon",
    "microsoft": "~/Games/Microsoft",
    "battlenet": "~/Games/Battlenet",
    "ubisoft": "~/Games/Ubisoft",
    "ea": "~/Games/EA",
}

# Root game directory — always checked so internal storage
# shows up even before any per-store subdirectories exist.
DEFAULT_GAMES_ROOT = "~/Games"

# Where the games.map lives by default. Steam Deck never
# relocates this without explicit user action, so the path is
# stable.
DEFAULT_GAMES_MAP = "~/.local/share/unifideck/games.map"




# ══════════════════════════════════════════════════════════════
# Pure helpers
# ══════════════════════════════════════════════════════════════


def _expand(path: str) -> str:
    """Expand ``~`` and ``$VAR`` references in a path string.

    Pure function — no filesystem I/O. Returns an absolute path
    (when the input is absolute or contains ``~``) or a relative
    path unchanged.

    Uses ``os.path.expandvars`` for env var substitution because
    ``pathlib.Path`` has no equivalent. The result is then
    wrapped through ``Path(...).expanduser()`` for the tilde
    resolution.
    """
    return str(Path(os.path.expandvars(path)).expanduser())


def _dedupe_paths(paths: list[str]) -> list[str]:
    """Remove duplicate paths preserving order.

    Two paths are considered equal if their normalized form
    (``os.path.normpath``) matches. Useful when merging
    discovery results from multiple sources that may overlap.
    """
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        norm = os.path.normpath(p)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(p)
    return out


def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Legacy alias for backward compatibility. Delegates to `get_cfg`."""
    return get_cfg(config, key, default)


# ══════════════════════════════════════════════════════════════
# Filesystem discovery
# ══════════════════════════════════════════════════════════════


def get_all_game_directories(config: ConfigManager | None = None) -> list[str]:
    """Return every directory that may contain installed games.

    Combines:

    1. Per-store install dirs from ``stores.<n>.install_dir``
       (or ``DEFAULT_INSTALL_DIRS`` as fallback)
    2. The user's custom path from ``download.custom_path``
    3. SD card / external drive mounts under
       ``paths.sd_card_root`` — scans 2 levels deep for
       ``Games/`` and ``GOG Games/`` folders

    Only returns directories that actually exist on disk.
    Result is deduplicated.
    """
    candidates: list[str] = []

    # 1. Root games directory — always available as internal storage.
    #    Create it if missing so internal storage is never empty.
    games_root = _expand(DEFAULT_GAMES_ROOT)
    _ensure_dir(games_root)
    candidates.append(games_root)

    # 2. Per-store install dirs
    for store, default in DEFAULT_INSTALL_DIRS.items():
        path = _cfg(config, f"stores.{store}.install_dir", default)
        candidates.append(_expand(path))

    # 3. Custom user path
    custom = get_cfg(config, "download.custom_path", "")
    if custom:
        candidates.append(_expand(custom))

    # 4. External drives — scan every writable non-system mount
    #    from /proc/mounts for Game directories.  No hardcoded
    #    paths — whatever is mounted and writable gets checked.
    candidates.extend(_scan_external_mounts())

    # Filter to existing dirs and dedupe
    existing = [p for p in candidates if Path(p).is_dir()]
    return _dedupe_paths(existing)


def _collect_game_dirs(parent_path: Path, effective_uid: int | None = None) -> list[str]:
    """Return ``Games/`` and ``GOG Games/`` subdirs of ``parent_path``.

    Symlinks are skipped to avoid loops. ``effective_uid`` threads
    through the demotion context from ``mounts.scan_mounts`` — a
    FUSE mount only visible via a demoted uid at enumeration time is
    still invisible to root for this per-subdir check too.
    """
    found: list[str] = []
    for sub in ("Games", "GOG Games"):
        p = parent_path / sub
        if mounts.mount_is_dir(str(p), effective_uid) and not _is_symlink(p, effective_uid):
            found.append(str(p))
    return found


def _is_symlink(path: Path, effective_uid: int | None) -> bool:
    """Demotion-aware ``is_symlink()`` — see ``mounts.mount_is_dir``."""
    if effective_uid is None:
        try:
            return path.is_symlink()
        except OSError:
            return False
    proc = mounts.run_demoted(["test", "-L", str(path)], effective_uid)
    return proc is not None and proc.returncode == 0


def _ensure_dir(path: str) -> None:
    """Create a directory if it doesn't exist. Idempotent."""
    Path(path).mkdir(parents=True, exist_ok=True)


def _scan_external_mounts() -> list[str]:
    """Scan every readable external mount for game directories.

    Delegates enumeration to ``mounts.scan_mounts`` (shared with
    ``rpc/mixins/storage.py`` and ``rpc/mixins/download.py`` — see
    that module for why a mount only visible to a non-root uid, e.g.
    a FUSE-mounted exFAT/NTFS card, still needs a demoted subprocess
    for every access, not just to notice it exists).
    ``require_writable=False``: this is discovering already-installed
    games, a read-only concern, not offering new install targets.
    """
    home_dev = mounts.stat_dev(str(Path.home()))
    found: list[str] = []
    for m in mounts.scan_mounts(home_dev, require_writable=False):
        found.extend(_collect_mount_game_dirs(Path(m.mount_point), m.effective_uid))
    return found


def _collect_mount_game_dirs(mp_path: Path, effective_uid: int | None = None) -> list[str]:
    """Game dirs at the mount root plus one level deeper.

    Some setups mount partitions inside a parent directory, so we
    also scan immediate children. Symlinks are skipped to avoid loops.
    """
    found = list(_collect_game_dirs(mp_path, effective_uid))
    for child in mounts.mount_child_dirs(str(mp_path), effective_uid):
        found.extend(_collect_game_dirs(child, effective_uid))
    return found


# ══════════════════════════════════════════════════════════════
# games.map location
# ══════════════════════════════════════════════════════════════


def get_games_map_path(config: ConfigManager | None = None) -> str:
    """Return the absolute path to the games.map file.

    Reads ``paths.games_map`` from config if set, otherwise
    falls back to ``~/.local/share/unifideck/games.map``. Tilde
    and env vars in the configured path are expanded.
    """
    raw = get_cfg(config, "paths.games_map", DEFAULT_GAMES_MAP)
    return _expand(raw)
