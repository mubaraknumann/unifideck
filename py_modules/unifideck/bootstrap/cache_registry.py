"""bootstrap.cache_registry — declare every named cache used by the plugin.

Called during ``_main`` BEFORE ``auto_discover()`` runs the
store constructors, because some stores call ``is_available()``
during construction which reads from the cache. Missing the
registration would cause a ``KeyError`` the first time a store
asks for its cache slot.

The TTL table is the single source of truth for which caches
exist and how long their entries survive. TTL semantics (from
``CacheManager``):

  - ``0`` — unbounded lifetime, entry survives until explicit
    invalidation (used for IDs and maps that don't expire by
    time)
  - positive integer — seconds until entry becomes stale

Every store in ``_STORE_CACHES`` below also gets one cache slot,
all TTL=0 — they're used to memoize the
per-store ``is_available`` result inside a single plugin session
so we don't re-probe every RPC call.
"""
from __future__ import annotations

from typing import Any

# Cache spec: (name, ttl_seconds). 0 means unbounded.
# Centralised so adding a cache = appending one tuple; no need
# to edit the bootstrap orchestrator.
#
# Registration here wins: ``CacheManager.register`` is idempotent,
# so later ``register`` calls with a different TTL (e.g.
# ``CompatLibrary.register`` reading ``cache_ttl.compat`` from user
# config) are no-ops — the ``defaults/config.json`` ``cache_ttl.*``
# keys are effectively dead.
#
# TTL policy for the sync-enrichment caches (metadata /
# steam_metadata / steam_reviews / unifidb_metadata): 30 days. A
# standard sync only fetches missing/new games; a force sync
# refreshes on demand. TTL expiry exists so entries (including
# negative markers) still self-heal without user action — the old
# 1-day/7-day TTLs made every sync past the window silently re-pull
# the WHOLE library ("10-minute sync a day later" reports).
_NAMED_CACHES: tuple[tuple[str, int], ...] = (
    ("steam_appid", 0),
    ("steam_real_appid", 0),
    # The title each ``steam_real_appid`` MISS was searched under. A
    # negative mapping means "no Steam counterpart for this title", and
    # the title can change under a stable game id — so the miss has to
    # record what it asked, or it silently becomes permanent.
    ("steam_appid_miss", 0),
    ("steam_metadata", 30 * 24 * 3600),
    # ``MetadataService`` caches the Steam ``appreviews`` summary
    # ({review_score, review_percentage, total_reviews}) per real
    # Steam AppID — feeds the native "Steam Review" library sort for
    # spoofed shortcuts via ``get_overview_enrichment``. Reviews
    # drift slowly; monthly is fresh enough, force sync refreshes
    # immediately.
    ("steam_reviews", 30 * 24 * 3600),
    # First-seen timestamp per shortcut AppID, stamped by reconcile
    # when a shortcut is first created. Drives the native "Date Added
    # to Library" sort. Unbounded — must never expire or a game would
    # appear "newly added" again.
    ("shortcut_added", 0),
    ("rawg_metadata", 86400),
    ("unifidb_metadata", 30 * 24 * 3600),
    ("metacritic", 604800),
    ("artwork_attempts", 0),
    ("game_sizes", 3600),
    ("compat", 0),
    # ``ArtworkService`` uses ``sgdb_fetch`` to record per-game
    # SGDB failure-cooldown timestamps — once a game totally misses
    # across all three phases (store + SGDB + Steam CDN), it gets a
    # 3600 s skip so repeated syncs don't hammer the SGDB API for
    # titles nobody has art for (delisted, obscure, etc.).
    ("sgdb_fetch", 3600),
    # ``MetadataService`` caches the merged-and-deduped metadata
    # under the ``"metadata"`` namespace (see ``CACHE_NAMESPACE``
    # in ``services/metadata_service.py``). Earlier this slot was
    # missing from the registry — ``_get_store("metadata")`` raised
    # ``ValueError: Cache 'metadata' not registered``, swallowed
    # by the service's try/except, so every ``enrich()`` call
    # silently re-fetched from all three upstream sources.
    ("metadata", 30 * 24 * 3600),
    # ``pcgw_backfill`` caches per-game save-location data fetched live
    # from PCGamingWiki when unifiDB has no entry (the hybrid fallback).
    # Save paths are very stable, so a long TTL avoids re-querying; the
    # 30-day expiry still lets genuinely-absent games re-check eventually.
    ("pcgw_saves", 30 * 24 * 3600),
)

_STORE_CACHES: tuple[str, ...] = (
    "epic", "gog", "amazon", "microsoft", "ubisoft", "battlenet",
    "gamevault",
)


def register_default_caches(cache: Any) -> None:
    """Declare every named + per-store cache on ``cache``.

    Args:
        cache: The ``CacheManager`` instance the Plugin holds on
            ``self.cache``. Mutated in place — every cache slot
            is registered via ``cache.register(name, ttl_seconds=N)``.

    Must be called before any store's constructor runs; see the
    module docstring for the ordering rationale.
    """
    for name, ttl in _NAMED_CACHES:
        cache.register(name, ttl_seconds=ttl)
    for store_name in _STORE_CACHES:
        cache.register(store_name, ttl_seconds=0)
