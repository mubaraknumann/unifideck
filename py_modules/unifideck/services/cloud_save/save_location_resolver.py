"""Resolve a game's save directory from enriched save-location metadata.

The unifiDB pipeline (``enrich_save_locations.py``) tags game records with
``save_locations`` — Ludusavi/PCGamingWiki paths like ``<winAppData>/Foo/Saves``
— plus per-store ``cloud`` support flags. Those flow through the metadata cache
(namespace ``"metadata"``, key ``"store:game_id"``) to here. A live-PCGamingWiki
fallback (``pcgw_saves`` cache, populated out-of-band by ``pcgw_backfill``) is
consulted when unifiDB has no entry.

This gives GOG/Epic save sync an authoritative location to use *instead of*
guessing by scanning the prefix for a folder whose name resembles the title.
It is consulted BELOW the store's own authoritative metadata (GOG remote-config,
legendary's ``cloud_save_folder``) and ABOVE that title-folder heuristic — so it
can't regress a game that already resolves correctly, but rescues the ones the
heuristic was missing.

Pure cache reads — no network, no event loop — so it is safe to call from the
synchronous ``get_local_save_dir`` on the launch hot path.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from unifideck.services.cloud_save.path_resolver import WinePrefixResolver

logger = logging.getLogger(__name__)


def _install_path_from_games_map(
    store: str, game_id: str, config: Any,
) -> str:
    """Authoritative install dir from games.map (handles custom locations).

    Users can install game files anywhere (SD card, external drive); the
    games.map ``work_dir`` records the actual chosen location per game, while
    the Wine prefix always stays under the plugin data dir. This is the right
    source for ``<base>``/``<root>`` (install-dir) save paths — a default-dir
    scan would miss games installed elsewhere.
    """
    try:
        from unifideck.services.shortcut.games_map import parse_games_map
        from unifideck.utils.paths import get_games_map_path
        path = get_games_map_path(config)
        if not path or not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as f:
            mapping = parse_games_map(f.read())
        entry = mapping.get(f"{store}:{game_id}")
        return entry.work_dir if entry and entry.work_dir else ""
    except Exception:  # pragma: no cover - best-effort
        return ""


def _read_cache(cache: Any, namespace: str, key: str) -> dict[str, Any] | None:
    if cache is None:
        return None
    try:
        data = cache.get(namespace, key)
    except Exception:  # pragma: no cover - cache is best-effort
        return None
    if isinstance(data, dict) and not data.get("_negative"):
        return data
    return None


def _save_locations_for(
    store: str, game_id: str, cache: Any,
) -> list[dict[str, Any]] | None:
    """Enriched save-location rows for a game: unifiDB metadata then PCGW cache."""
    key = f"{store}:{game_id}"
    for namespace in ("metadata", "pcgw_saves"):
        data = _read_cache(cache, namespace, key)
        if data and data.get("save_locations"):
            rows: list[dict[str, Any]] = data["save_locations"]
            return rows
    return None


def _is_foreign_cloud_path(path: str) -> bool:
    """True for a store-cloud MIRROR path (e.g. Steam
    ``<root>/userdata/<storeUserId>/<appid>/remote``).

    These are where a STORE syncs the game's saves for ITS OWN copy — a game
    under our launcher never reads/writes there, and we can't resolve the
    foreign account id anyway. Everything else (install-dir ``<base>``, AppData,
    Documents …) is store-AGNOSTIC even when Ludusavi happened to tag it for one
    store, so it stays a valid candidate.
    """
    p = path.lower()
    return "userdata" in p and "<storeuserid>" in p


def _os_compatible(loc: dict[str, Any], native_linux: bool) -> bool:
    """Whether a row's ``os`` tag matches how the game runs (native-Linux vs
    Proton/Windows). A row with no ``os`` applies to any OS."""
    oses = loc.get("os") or []
    if not oses:
        return True
    if native_linux:
        return "linux" in oses
    return "windows" in oses or "dos" in oses


def _select_rows(
    locations: list[dict[str, Any]], store: str, native_linux: bool = False,
) -> list[dict[str, Any]]:
    """Order rows best-first for ``store``; keep foreign-tagged paths as backup.

    A path tagged for another store but using a store-agnostic token (Half-Life
    2's ``<base>/hl2/save`` is tagged ``steam`` yet a GOG copy saves there too)
    is KEPT as a lower-priority fallback. Only genuine store-cloud mirror paths
    (Steam ``userdata/<id>/<appid>/remote``) are demoted to last. Priority:
    OS-matched (native-Linux vs Windows-prefix) → store-matched/generic →
    other-store backup; ``save`` before ``config``; real before cloud-mirror.
    """
    rows = [
        loc for loc in locations
        if isinstance(loc, dict) and loc.get("path")
    ]

    def sort_key(loc: dict[str, Any]) -> tuple[int, int, int, int]:
        stores = loc.get("stores") or []
        tags = loc.get("tags") or []
        foreign_cloud = _is_foreign_cloud_path(loc.get("path", ""))
        store_rank = 0 if (not stores or store in stores) else 1
        return (
            0 if _os_compatible(loc, native_linux) else 1,  # right-OS paths first
            1 if foreign_cloud else 0,   # cloud-mirror paths last
            store_rank,                  # our store / generic before other stores
            0 if "save" in tags else 1,  # save before config
        )

    rows.sort(key=sort_key)
    return rows


def _is_safe_save_dir(candidate: str, install_path: str) -> bool:
    """Whether ``candidate`` can be a save directory at all.

    Rows like ``<base>/SAVE*.SAV`` stop at their wildcard and resolve to the
    install folder itself, and ``<base>/EOBDATA.SAV`` resolves to a file. A
    forced download first empties the save dir and an upload sends all of
    it, so the install folder (or anything containing it) would mean
    deleting the game or uploading it whole. Rejected here, logged so the
    gap stays visible.
    """
    if os.path.isfile(candidate):
        reason = "is a file"
    elif install_path and _contains(candidate, install_path):
        reason = "contains the install folder"
    else:
        return True
    logger.info("[SaveLoc] rejected save dir %s: %s", candidate, reason)
    return False


def _contains(outer: str, inner: str) -> bool:
    """True when ``inner`` is ``outer`` or lies inside it."""
    outer_p = os.path.realpath(outer)
    inner_p = os.path.realpath(inner)
    return inner_p == outer_p or inner_p.startswith(outer_p.rstrip(os.sep) + os.sep)


def resolve_save_dir(
    store: str,
    game_id: str,
    *,
    prefix_path: str,
    install_path: str = "",
    config: Any = None,
    cache: Any = None,
    native_linux: bool = False,
) -> str | None:
    """Resolve the best enriched save directory, or ``None`` if unavailable.

    Prefers a resolved candidate that already exists on disk; otherwise returns
    the first resolvable candidate (sync will create it). ``install_path`` (for
    ``<base>`` install-dir saves) defaults to the games.map ``work_dir`` so
    user-chosen install locations resolve correctly. ``native_linux`` resolves
    against real Linux home/XDG dirs (GOG native builds) instead of the Wine
    prefix, and prefers Linux-tagged rows.
    """
    locations = _save_locations_for(store, game_id, cache)
    if not locations:
        return None
    if not install_path:
        install_path = _install_path_from_games_map(store, game_id, config)
    candidates: list[str] = []
    for loc in _select_rows(locations, store, native_linux):
        resolved = WinePrefixResolver.resolve_ludusavi_path(
            loc["path"], prefix_path, install_path, native_linux=native_linux,
        )
        if (
            resolved and resolved not in candidates
            and _is_safe_save_dir(resolved, install_path)
        ):
            candidates.append(resolved)
    for candidate in candidates:
        if os.path.isdir(candidate):
            logger.info(
                "[SaveLoc] %s:%s resolved on-disk via enriched metadata: %s",
                store, game_id, candidate,
            )
            return candidate
    if candidates:
        logger.info(
            "[SaveLoc] %s:%s resolved (not yet on disk) via enriched metadata: %s",
            store, game_id, candidates[0],
        )
        return candidates[0]
    return None
