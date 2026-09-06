"""rpc/mixins/_metadata_display.py — Helpers for ``get_game_metadata_display``.

Extracted from :mod:`ui` to keep that file under the 550-LOC
volumetry cap. The split is along a natural seam:

* :mod:`ui` (``UIRPCMixin``) owns the RPC method that the frontend
  invokes — the thin "this is a JS-callable endpoint" wrapper.
* This module owns the *content* — every helper that reads from a
  cache namespace, picks a field across sources, or formats a
  fallback URL. None of them touch ``self`` or the RPC layer, so
  they're plain module-level functions and easy to unit-test in
  isolation.

The leading underscore in the module name marks the public surface
as internal: nothing outside :mod:`unifideck.rpc.mixins.ui` should
import from here.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.core.compat_bridge import appid_candidates
from unifideck.core.store_urls import store_search_url

from ._compat_payload import active_track, compat_block

logger = logging.getLogger(__name__)

# Re-exported for the existing importers of this module. The table
# itself moved to ``core.store_urls`` so the launcher can reach it
# without importing ``rpc`` (import-linter's ``rpc-is-leaf``).
__all__ = ["store_search_url"]


def build_game_from_info(info: dict[str, Any], app_id: int) -> Any:
    """Reconstruct a ``Game`` instance from ``sync.get_game_info``'s asdict.

    The reconstruct is necessary because ``MetadataService.enrich``
    accepts a ``Game`` dataclass (its cache key is derived from
    ``game.store + game.store_game_id``), not the dict form the
    sync RPC returns. Defaults match the dataclass's own defaults
    so a half-populated entry doesn't crash the rebuild.
    """
    from unifideck.core.types import Game
    return Game(
        app_id=info.get("app_id", app_id),
        store=info.get("store", ""),
        store_game_id=info.get("store_game_id", ""),
        title=info.get("title", ""),
        installed=info.get("installed", False),
        install_path=info.get("install_path"),
        exe_path=info.get("exe_path"),
        size_bytes=info.get("size_bytes", 0),
        tags=list(info.get("tags") or []),
        icon_url=info.get("icon_url"),
        hero_url=info.get("hero_url"),
        logo_url=info.get("logo_url"),
        metadata=dict(info.get("metadata") or {}),
    )


async def safe_enrich(metadata: Any, game: Any, app_id: int) -> dict[str, Any]:
    """Run ``MetadataService.enrich`` swallowing any exception."""
    if metadata is None:
        return {}
    try:
        return await metadata.enrich(game) or {}
    except Exception as exc:
        logger.debug(
            "[MetadataDisplay] enrich failed for %d: %s", app_id, exc,
        )
        return {}


async def resolve_steam_payload(
    cache: Any, metadata: Any, game: Any, app_id: int,
) -> tuple[int, dict[str, Any]]:
    """Resolve the shortcut → Steam-AppID + cached appdetails pair.

    First reads the caches that the sync's enrichment populated;
    when either is cold or stale, falls back to a live
    ``fetch_appdetails_for_game`` call so the panel renders
    correctly on first open for games that synced before the
    Steam appdetails cache had time to fill. Exceptions are
    swallowed at DEBUG level — empty fields are the right
    fallback for the UI.
    """
    steam_app_id = read_steam_real_appid(cache, app_id)
    steam_meta = read_steam_metadata(cache, steam_app_id)
    if metadata is None:
        return steam_app_id, steam_meta
    if steam_app_id and steam_meta and steam_meta.get("name"):
        return steam_app_id, steam_meta
    try:
        fresh = await metadata.fetch_appdetails_for_game(game)
    except Exception as exc:
        logger.debug(
            "[MetadataDisplay] fetch_appdetails_for_game failed for %d: %s",
            app_id, exc,
        )
        return steam_app_id, steam_meta
    if not fresh:
        return steam_app_id, steam_meta
    steam_app_id = read_steam_real_appid(cache, app_id)
    steam_meta = read_steam_metadata(cache, steam_app_id)
    return steam_app_id, steam_meta


def read_cache_store(cache: Any, namespace: str) -> dict[str, Any]:
    """Return the raw ``_data`` dict of a cache namespace, or empty.

    Reads the same ``_stores`` attribute that
    ``StoreRPCMixin.get_steam_metadata_cache`` uses, so a cache
    miss here matches the visible behaviour of that RPC for the
    same key.
    """
    stores = getattr(cache, "_stores", None)
    if not isinstance(stores, dict):
        return {}
    store = stores.get(namespace)
    data = getattr(store, "_data", None)
    return data if isinstance(data, dict) else {}


# ``appid_candidates`` was defined here. It is now
# ``core.compat_bridge.appid_candidates`` — the same six lines existed
# three times under three different names (here, as
# ``compatibility/library._appid_key_candidates``, and inlined in
# ``core/sync_queries_mixin.get_game_info``), which is why check 11's
# name-exact matching never saw them. Audit register item 20.


def read_steam_real_appid(cache: Any, shortcut_app_id: int) -> int:
    """Resolve the shortcut → real-Steam-AppID mapping.

    Populated by ``MetadataService.fetch_appdetails_for_game``
    during sync. Returns ``0`` when the shortcut hasn't been
    mapped yet. Tries both signed and unsigned forms of the AppID.
    """
    data = read_cache_store(cache, "steam_real_appid")
    for key in appid_candidates(shortcut_app_id):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return 0


def read_steam_metadata(cache: Any, steam_app_id: int) -> dict[str, Any]:
    """Return the cached Steam ``appdetails`` payload, or empty.

    Keyed by the real Steam AppID (not the shortcut). Returns
    empty when ``steam_app_id == 0`` or the cache hasn't been
    populated for this game.
    """
    if not steam_app_id:
        return {}
    data = read_cache_store(cache, "steam_metadata")
    entry = data.get(str(steam_app_id))
    return entry if isinstance(entry, dict) else {}


def read_compat_entry(
    cache: Any, shortcut_app_id: int, steam_app_id: int = 0,
) -> dict[str, Any]:
    """Return the ``compat`` cache entry for a game, or empty.

    ``CompatLibrary`` caches by real Steam AppID (the
    ``appid`` passed to ``get_for_appid``), not by the
    Unifideck synthetic shortcut ID. When ``steam_app_id`` is
    provided and positive, we look it up directly — this
    covers the common case where the compat phase already
    resolved the title. Falls back to the signed/unsigned
    shortcut-ID scan for callers that don't have the real
    Steam ID yet.
    """
    data = read_cache_store(cache, "compat")
    if steam_app_id > 0:
        entry = data.get(str(steam_app_id))
        if isinstance(entry, dict):
            return entry
    for key in appid_candidates(shortcut_app_id):
        entry = data.get(key)
        if isinstance(entry, dict):
            return entry
    return {}


def has_steam_store_page(
    steam_meta: dict[str, Any], steam_app_id: int,
) -> bool:
    """Validate that ``steam_meta`` corresponds to a real Steam page.

    Three-field guard (matches staging): ``type`` is ``"game"`` or
    ``"application"`` (rules out DLC / demos), embedded
    ``steam_appid`` agrees with the lookup key, and the entry has a
    ``name``. Prevents spoofed-only entries from showing DLC /
    Community / Points / Discussions / Guides buttons that would
    404 in the Steam client.
    """
    if not steam_app_id or not steam_meta:
        return False
    meta_type = str(steam_meta.get("type", "")).lower()
    try:
        meta_appid = int(steam_meta.get("steam_appid", 0) or 0)
    except (TypeError, ValueError):
        meta_appid = 0
    meta_name = str(steam_meta.get("name", "")).strip()
    return (
        meta_type in ("game", "application")
        and meta_appid == steam_app_id
        and bool(meta_name)
    )


def pick_developer(
    steam_meta: dict[str, Any], enriched: dict[str, Any],
) -> str:
    """Prefer Steam Store ``developers`` list; fall back to UnifiDB."""
    devs = steam_meta.get("developers") if isinstance(steam_meta, dict) else None
    if isinstance(devs, list) and devs:
        return ", ".join(str(d) for d in devs if d)
    return str(enriched.get("developer") or "")


def pick_publisher(
    steam_meta: dict[str, Any], enriched: dict[str, Any],
) -> str:
    """Prefer Steam Store ``publishers`` list; fall back to UnifiDB."""
    pubs = steam_meta.get("publishers") if isinstance(steam_meta, dict) else None
    if isinstance(pubs, list) and pubs:
        return ", ".join(str(p) for p in pubs if p)
    return str(enriched.get("publisher") or "")


def pick_description(
    steam_meta: dict[str, Any], enriched: dict[str, Any],
) -> str:
    """Best available synopsis: Steam short → detailed → UnifiDB → Metacritic."""
    if isinstance(steam_meta, dict):
        short = steam_meta.get("short_description")
        if isinstance(short, str) and short.strip():
            return short
        detailed = steam_meta.get("detailed_description")
        if isinstance(detailed, str) and detailed.strip():
            return detailed
    desc = enriched.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc
    summary = enriched.get("summary")
    return summary if isinstance(summary, str) else ""


def pick_release_date(
    steam_meta: dict[str, Any], enriched: dict[str, Any],
) -> str:
    """Best available release-date string (Steam nested dict → UnifiDB flat)."""
    if isinstance(steam_meta, dict):
        rd = steam_meta.get("release_date")
        if isinstance(rd, dict):
            date = rd.get("date")
            if isinstance(date, str) and date:
                return date
    fallback = enriched.get("release_date")
    return fallback if isinstance(fallback, str) else ""


def pick_metacritic(
    steam_meta: dict[str, Any], enriched: dict[str, Any],
) -> int | None:
    """Return the Metacritic critic score, or ``None``."""
    if isinstance(steam_meta, dict):
        mc = steam_meta.get("metacritic")
        if isinstance(mc, dict):
            score = mc.get("score")
            if isinstance(score, int):
                return score
    score = enriched.get("metacritic_score")
    return score if isinstance(score, int) else None


def pick_genres(
    steam_meta: dict[str, Any], enriched: dict[str, Any],
) -> list[str]:
    """Extract genre labels (Steam objects → strings, UnifiDB already flat)."""
    if isinstance(steam_meta, dict):
        raw = steam_meta.get("genres")
        if isinstance(raw, list):
            labels = [
                str(g.get("description", "")).strip()
                for g in raw
                if isinstance(g, dict) and g.get("description")
            ]
            if labels:
                return labels
    fallback = enriched.get("genres")
    if isinstance(fallback, list):
        return [str(g).strip() for g in fallback if g]
    return []


def build_payload(
    game: Any,
    enriched: dict[str, Any],
    steam_app_id: int,
    steam_meta: dict[str, Any],
    compat_entry: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the full ``GameMetadata`` dict the frontend consumes.

    Concentrates every cross-source pick into one site so
    ``get_game_metadata_display`` stays under the function-LOC and
    fan-out caps. Field order matches the TS interface in
    ``src/types/api.ts`` — easy to spot-check via diff.
    """
    homepage = steam_meta.get("website") if isinstance(steam_meta, dict) else None
    return {
        "steam_app_id": steam_app_id,
        "has_steam_store_page": has_steam_store_page(steam_meta, steam_app_id),
        **storefront_fields(game, enriched),
        "developer": pick_developer(steam_meta, enriched),
        "publisher": pick_publisher(steam_meta, enriched),
        "release_date": pick_release_date(steam_meta, enriched),
        "metacritic": pick_metacritic(steam_meta, enriched),
        "description": pick_description(steam_meta, enriched),
        # Every device's rating, plus which one this device is, so the
        # panel can name the hardware it is describing.
        "compat_device": active_track(),
        "compat": compat_block(compat_entry),
        "genres": pick_genres(steam_meta, enriched),
        "homepage_url": homepage,
    }


def storefront_fields(game: Any, enriched: dict[str, Any]) -> dict[str, Any]:
    """The which-storefront-is-this block of the payload.

    Grouped into one helper so ``build_payload`` stays inside the fan-out cap
    (adding ``cloud_saves`` as a separate call pushed it to 11). Everything
    here answers "which store's copy is this, and what does that copy offer".
    """
    return {
        "store": game.store,
        "store_url": store_search_url(game.store, game.title),
        "title": game.title,
        "cloud_saves": pick_cloud_saves(
            enriched, game.store, getattr(game, "store_game_id", "") or "",
        ),
    }


def pick_cloud_saves(
    enriched: dict[str, Any], store: str, game_id: str = "",
) -> bool | None:
    """Whether ``store``'s copy of this game has native cloud saves.

    Delegates to the shared resolver so this and the cloud-save button's
    ``cloud_supported`` can never disagree — Epic answers from its own cached
    metadata, everything else from the unifiDB catalog. No install, no prefix
    and no store CLI, which is what lets the panel answer for a game the user
    has not downloaded yet.

    ``None`` means "unknown", and the UI says so rather than claiming an
    absence it cannot back up.
    """
    from unifideck.services.cloud_save.support import resolve_cloud_support
    return resolve_cloud_support(store, game_id, enriched)


