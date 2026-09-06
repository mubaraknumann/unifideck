"""Marker-based sweep of Unifideck-installed game directories.

Every Unifideck install drops an *ownership marker* in the game folder:

* ``.unifideck-id``            — GOG (carries ``game_id``);
* ``.unifideck_manifest.json`` — Epic / Amazon (carries ``store`` +
  ``store_id``);
* ``.unifideck_ubisoft``       — Ubisoft (carries ``space_id``).

The marker is definitive proof Unifideck created the directory, which lets
cleanup reliably remove games installed to *any* location — including custom
folders and SD-card libraries the store CLIs don't scan — plus orphans left
behind when a store-CLI uninstall removes the game's own files but not our
marker (an empty stub dir). It never touches a folder we didn't create.

Why this exists: GOG resolves a game's install dir by scanning only its
configured ``download_dir``; a game installed elsewhere can't be resolved,
so its uninstall silently no-ops and the full install stays on disk. And
the destructive "Delete all data" only removed dirs recorded in
``games.map``, so orphans accumulated.

Roots are *derived from the install records Unifideck already keeps* (the
store CLIs' ``installed.json`` + ``games.map``) — never a blind filesystem
walk. All functions are synchronous / blocking; call from a thread.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from unifideck.core.safe_delete import safe_rmtree

logger = logging.getLogger(__name__)

_GOG_MARKER = ".unifideck-id"
# Written by ``core/manifest.py``'s ``write_manifest`` (from
# ``epic/install.py`` and ``amazon/amazon_install.py``). This module is its
# ONLY reader — nothing in ``core/manifest.py`` reads it back — so a search
# there for consumers turns up nothing and the write looks dead. It is not:
# see the note in that module's docstring before touching either side.
_MANIFEST_MARKER = ".unifideck_manifest.json"
# Ubisoft's install marker, written by ``stores/ubisoft/library/
# detection_helpers.py`` (``write_install_marker`` / ``write_marker_sync``).
# Ubisoft games usually live *inside* their prefix, which cleanup removes
# separately — and ``collect_install_roots`` deliberately drops any root
# under ``prefixes/``. This covers the other case: a standalone install dir,
# including a sibling orphan under a root ``games.map`` already knows.
_UBISOFT_MARKER = ".unifideck_ubisoft"
_ALL_MARKERS = (_GOG_MARKER, _MANIFEST_MARKER, _UBISOFT_MARKER)
# Library roots hold each game one level down (``<root>/<Game>/<marker>``),
# so a depth-1 glob is enough and stays bounded (no deep recursion into
# multi-GB game trees).
_MIN_ROOT_DEPTH = 4

def collect_install_roots() -> set[Path]:
    """Library roots where Unifideck has recorded installs.

    Union of the parent directories of every install path in nile's and
    legendary's ``installed.json`` and our ``games.map``. These records
    survive the cleanup's auth wipe (the CLI configs are Heroic-shared and
    left intact), but ``games.map`` is deleted by the data-dir wipe — so
    callers that wipe state must collect roots *before* that step.
    """
    roots: set[Path] = set()
    _add_paths(roots, _nile_install_paths())
    _add_paths(roots, _legendary_install_paths())
    _add_paths(roots, _games_map_work_dirs())
    return {
        r for r in roots
        if len(r.parts) >= _MIN_ROOT_DEPTH
        and "/prefixes/" not in str(r)
        and r.is_dir()
    }

def _add_paths(roots: set[Path], paths: Iterator[str]) -> None:
    for p in paths:
        if p:
            roots.add(Path(p).expanduser().parent)

def _read_json(path: str) -> object:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def _nile_install_paths() -> Iterator[str]:
    data = _read_json("~/.config/nile/installed.json")
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("path"):
                yield str(entry["path"])

def _legendary_install_paths() -> Iterator[str]:
    data = _read_json("~/.config/legendary/installed.json")
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict) and entry.get("install_path"):
                yield str(entry["install_path"])

def _games_map_work_dirs() -> Iterator[str]:
    try:
        text = Path(
            "~/.local/share/unifideck/games.map",
        ).expanduser().read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        # store:id=exe<TAB>work_dir<TAB>app_id
        fields = line.split("=", 1)[1].split("\t")
        if len(fields) >= 2 and fields[1]:
            yield fields[1]

def _parse_marker(marker: Path) -> tuple[str, str] | None:
    """Return ``(store, game_id)`` recorded in a marker file, or None."""
    data = _read_json(str(marker))
    if not isinstance(data, dict):
        return None
    if marker.name == _GOG_MARKER:
        gid = data.get("game_id") or data.get("gameId")
        return ("gog", str(gid)) if gid else None
    if marker.name == _UBISOFT_MARKER:
        # ``space_id`` is Ubisoft's game id; the marker also carries
        # ``install_path`` / ``game_title`` / ``executable``, unused here.
        space_id = data.get("space_id")
        return ("ubisoft", str(space_id)) if space_id else None
    store, gid = data.get("store"), data.get("store_id")
    return (str(store), str(gid)) if store and gid else None

def iter_marked_dirs(
    roots: set[Path],
) -> Iterator[tuple[Path, str, str]]:
    """Yield ``(dir, store, game_id)`` for each marked dir under *roots*."""
    seen: set[Path] = set()
    for root in roots:
        for marker_name in _ALL_MARKERS:
            for marker in root.glob(f"*/{marker_name}"):
                game_dir = marker.parent
                if game_dir in seen:
                    continue
                parsed = _parse_marker(marker)
                if parsed is None:
                    continue
                seen.add(game_dir)
                yield game_dir, parsed[0], parsed[1]

def find_for_game(
    roots: set[Path], store: str, game_id: str,
) -> Path | None:
    """The marked install dir for a specific ``(store, game_id)``, if any."""
    for game_dir, mstore, mgid in iter_marked_dirs(roots):
        if mstore == store and mgid == game_id:
            return game_dir
    return None

def sweep_game(store: str, game_id: str) -> bool:
    """Delete the marked install dir for one game (per-game fallback).

    Used after a store's own uninstall to guarantee the directory is gone
    even when the store couldn't resolve it (e.g. a GOG game installed
    outside ``download_dir``). No-op — and returns True — when no marked
    dir matches (the store already deleted it).
    """
    target = find_for_game(collect_install_roots(), store, game_id)
    if target is None:
        return True
    logger.info(
        "[marker_sweep] removing %s install dir by marker: %s",
        store, target,
    )
    return safe_rmtree(target)

def sweep_all(roots: set[Path]) -> int:
    """Delete every marked install dir under *roots*. Returns the count.

    The destructive "Delete all data" sweep — removes currently-installed
    Unifideck games, orphan stubs, and out-of-default-root installs alike,
    while leaving any folder without our marker untouched.
    """
    count = 0
    for game_dir, _store, _gid in list(iter_marked_dirs(roots)):
        if safe_rmtree(game_dir):
            count += 1
    if count:
        logger.info("[marker_sweep] removed %d marked install dirs", count)
    return count
