"""Owned Steam games detection.

Walks the user's Steam config to enumerate apps already owned
on the native Steam account so we can avoid re-importing them
as non-Steam shortcuts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from unifideck.metadata.unifidb import normalize_title_for_matching

from .library import find_steam_path

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
_ACF_NAME_PATTERN = re.compile(r'"name"\s+"([^"]*)"')
_APPID_PATTERN = re.compile(r'"appid"\s+"(\d+)"')
_LIBFOLDER_PATH_PATTERN = re.compile(r'"path"\s+"([^"]*)"')
_Fingerprint = tuple[str, float | None, tuple[tuple[str, float | None], ...]]
_cache: tuple[_Fingerprint, frozenset[str]] | None = None
_appid_cache: tuple[_Fingerprint, dict[str, OwnedApp]] | None = None
# Owned-but-not-installed titles can't be read from appmanifests, so the
# frontend (which can enumerate the full Steam library via collectionStore)
# pushes them here for the backend filter to read.
_FRONTEND_CACHE_PATH = Path(
    "~/.local/share/unifideck/steam_owned_titles.json",
).expanduser()
# Companion to _FRONTEND_CACHE_PATH: same push, but keeps the real Steam
# appid alongside each title. Separate file so load_frontend_owned_titles's
# existing shape/consumers (stores/ubisoft/library/steam_filter.py) are
# untouched by this — see save_frontend_owned_games.
_FRONTEND_GAMES_CACHE_PATH = Path(
    "~/.local/share/unifideck/steam_owned_games.json",
).expanduser()


class OwnedApp(NamedTuple):
    """A real Steam appid plus the *original* (non-normalised) title it
    was matched from.

    The original title is what lets a caller recover an edition label
    (``extract_edition_label`` needs case/punctuation-preserving text —
    the normalised form used as the lookup key has already lost that).
    Without this, the detail-page store switcher's synthetic Steam entry
    had no edition text to show at all, e.g. Disco Elysium's Steam copy
    reading "Standard Edition" instead of "The Final Cut" even though
    the Steam listing carries that same edition name.
    """

    appid: int
    title: str


def save_frontend_owned_titles(raw_titles: list[str]) -> int:
    """Persist the frontend-supplied Steam-owned game titles (normalised).

    Stores the shared-normaliser form so the filter reads them directly.
    Atomic write; returns the number of titles stored.
    """
    normalized = sorted({
        n for t in raw_titles
        if isinstance(t, str) and (n := normalize_title_for_matching(t))
    })
    payload = {"updated": time.time(), "titles": normalized}
    try:
        _FRONTEND_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FRONTEND_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _FRONTEND_CACHE_PATH)
    except OSError as e:
        logger.warning("[owned_games] could not write owned-titles cache: %s", e)
        return 0
    logger.info(
        "[owned_games] stored %d frontend-supplied owned Steam title(s)",
        len(normalized),
    )
    return len(normalized)


def load_frontend_owned_titles() -> frozenset[str]:
    """Read the frontend-supplied owned titles (empty when absent/stale)."""
    try:
        data = json.loads(_FRONTEND_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    titles = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(titles, list):
        return frozenset()
    return frozenset(t for t in titles if isinstance(t, str) and t)


def save_frontend_owned_games(raw_games: list[Any]) -> int:
    """Persist frontend-supplied ``{title, appid}`` owned-Steam-game pairs.

    Companion to :func:`save_frontend_owned_titles` — same purpose
    (owned-but-not-installed titles the frontend reads from
    ``collectionStore``, invisible to the appmanifest scan) but keeps the
    real Steam appid too. Title-only isn't enough for the cross-store
    duplicate grouping in :mod:`core.game_grouping`, which needs an
    actual appid to point the detail-page store switcher at. Written to
    its own file so :func:`load_frontend_owned_titles`'s shape and its
    existing consumer (``stores/ubisoft/library/steam_filter.py``) stay
    untouched.

    The ORIGINAL (non-normalised) title is stored too, not just used as
    a lookup source — see :class:`OwnedApp`.
    """
    normalized: dict[str, OwnedApp] = {}
    for g in raw_games:
        if not isinstance(g, dict):
            continue
        title, appid = g.get("title"), g.get("appid")
        if not isinstance(title, str) or not isinstance(appid, (int, float)):
            continue
        n = normalize_title_for_matching(title)
        if n:
            normalized[n] = OwnedApp(appid=int(appid), title=title)
    payload = {
        "updated": time.time(),
        "games": {k: {"appid": v.appid, "title": v.title} for k, v in normalized.items()},
    }
    try:
        _FRONTEND_GAMES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FRONTEND_GAMES_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _FRONTEND_GAMES_CACHE_PATH)
    except OSError as e:
        logger.warning("[owned_games] could not write owned-games cache: %s", e)
        return 0
    logger.info(
        "[owned_games] stored %d frontend-supplied owned Steam game(s) "
        "with appid",
        len(normalized),
    )
    return len(normalized)


def load_frontend_owned_games() -> dict[str, OwnedApp]:
    """Read the frontend-supplied ``{normalized title: OwnedApp}`` map."""
    try:
        data = json.loads(_FRONTEND_GAMES_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    games = data.get("games") if isinstance(data, dict) else None
    if not isinstance(games, dict):
        return {}
    result: dict[str, OwnedApp] = {}
    for k, v in games.items():
        if not isinstance(k, str):
            continue
        # Back-compat: an older cache wrote plain ints, not {appid, title}.
        if isinstance(v, int):
            result[k] = OwnedApp(appid=v, title=k)
        elif (
            isinstance(v, dict)
            and isinstance(v.get("appid"), int)
            and isinstance(v.get("title"), str)
        ):
            result[k] = OwnedApp(appid=v["appid"], title=v["title"])
    return result


def get_owned_app_ids(
    config: ConfigManager | None = None,
) -> dict[str, OwnedApp]:
    """Normalized title -> :class:`OwnedApp`, for installed native games.

    Companion to :func:`get_owned_titles` (title-only) — separately
    cached (same ``.acf`` fingerprint) so that function's return shape
    and existing consumer are untouched.
    """
    global _appid_cache
    steam_path = find_steam_path(config)
    if steam_path is None:
        return {}
    fingerprint = _compute_fingerprint(Path(steam_path))
    if _appid_cache is not None and _appid_cache[0] == fingerprint:
        return _appid_cache[1]
    mapping: dict[str, OwnedApp] = {}
    for library_root in _list_library_roots(Path(steam_path)):
        try:
            mapping.update(_games_from_library(library_root))
        except OSError as e:
            logger.warning(
                "[owned_games] could not scan %s for appids: %s",
                library_root, e,
            )
    _appid_cache = (fingerprint, mapping)
    return mapping


def get_all_owned_app_ids(
    config: ConfigManager | None = None,
) -> dict[str, OwnedApp]:
    """Combined installed + frontend-supplied ``{title: OwnedApp}`` map.

    The single entry point :mod:`core.game_grouping` reads to know which
    Unifideck-managed titles are also owned on real Steam. Frontend
    values win on overlap — same source of truth the display layer
    itself reads from, and freshest for a title that just got installed.
    """
    combined = dict(get_owned_app_ids(config))
    combined.update(load_frontend_owned_games())
    return combined


def get_owned_titles(
    config: ConfigManager | None = None,
) -> frozenset[str]:
    """Get owned titles."""
    global _cache
    steam_path = find_steam_path(config)
    if steam_path is None:
        logger.debug("[owned_games] no Steam install found")
        return frozenset()
    fingerprint = _compute_fingerprint(Path(steam_path))
    if _cache is not None and _cache[0] == fingerprint:
        return _cache[1]
    titles = _scan_all_libraries(Path(steam_path))
    logger.info(
        "[owned_games] indexed %d Steam-native title(s) from %s",
        len(titles), steam_path,
    )
    _cache = (fingerprint, titles)
    return titles
def invalidate_cache() -> None:
    """Invalidate cache."""
    global _cache, _appid_cache
    _cache = None
    _appid_cache = None
def _compute_fingerprint(steam_path: Path) -> _Fingerprint:
    """Compute fingerprint."""
    libfolders_vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    libfolders_mtime = _stat_mtime(libfolders_vdf)
    library_roots = _list_library_roots(steam_path)
    per_library: list[tuple[str, float | None]] = []
    for root in library_roots:
        steamapps = root / "steamapps"
        per_library.append((str(root), _stat_mtime(steamapps)))
    return (str(steam_path), libfolders_mtime, tuple(per_library))
def _stat_mtime(path: Path) -> float | None:
    """Stat mtime."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None

def _scan_all_libraries(steam_path: Path) -> frozenset[str]:
    """Scan all libraries."""
    titles: set[str] = set()
    for library_root in _list_library_roots(steam_path):
        try:
            titles.update(_titles_from_library(library_root))
        except OSError as e:
            logger.warning(
                "[owned_games] could not scan %s: %s",
                library_root, e,
            )
    return frozenset(titles)
def _list_library_roots(steam_path: Path) -> list[Path]:
    """List library roots."""
    roots: list[Path] = [steam_path]
    libfolders_vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    if not libfolders_vdf.is_file():
        return roots
    try:
        content = libfolders_vdf.read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError as e:
        logger.warning(
            "[owned_games] cannot read %s: %s", libfolders_vdf, e,
        )
        return roots
    for match in _LIBFOLDER_PATH_PATTERN.finditer(content):
        candidate = Path(match.group(1))
        if candidate == steam_path:
            continue
        if (candidate / "steamapps").is_dir():
            roots.append(candidate)
    return roots
def _titles_from_library(library_root: Path) -> set[str]:
    """Titles from library."""
    steamapps = library_root / "steamapps"
    if not steamapps.is_dir():
        return set()
    titles: set[str] = set()
    for manifest in steamapps.glob("appmanifest_*.acf"):
        title = _extract_name_from_manifest(manifest)
        if title:
            normalized = normalize_title_for_matching(title)
            if normalized:
                titles.add(normalized)
    return titles
def _extract_name_from_manifest(manifest: Path) -> str | None:
    """Extract name from manifest."""
    try:
        content = manifest.read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError as e:
        logger.warning(
            "[owned_games] cannot read %s: %s", manifest, e,
        )
        return None
    match = _ACF_NAME_PATTERN.search(content)
    return match.group(1) if match else None


def _games_from_library(library_root: Path) -> dict[str, OwnedApp]:
    """Normalized title -> :class:`OwnedApp` for every manifest in one
    library root.

    Same ``.acf`` glob as :func:`_titles_from_library`, run separately
    (rather than threaded through it) so that function's return shape
    stays a plain ``set[str]`` for its existing caller.
    """
    steamapps = library_root / "steamapps"
    if not steamapps.is_dir():
        return {}
    mapping: dict[str, OwnedApp] = {}
    for manifest in steamapps.glob("appmanifest_*.acf"):
        try:
            content = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("[owned_games] cannot read %s: %s", manifest, e)
            continue
        name_match = _ACF_NAME_PATTERN.search(content)
        appid_match = _APPID_PATTERN.search(content)
        if not name_match or not appid_match:
            continue
        original_title = name_match.group(1)
        normalized = normalize_title_for_matching(original_title)
        if normalized:
            mapping[normalized] = OwnedApp(
                appid=int(appid_match.group(1)), title=original_title,
            )
    return mapping
