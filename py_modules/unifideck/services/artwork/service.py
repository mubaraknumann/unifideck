"""services/artwork/service.py — Game artwork fetcher.

EventBus subscriber that downloads game artwork from SteamGridDB
and writes files to Steam's grid/ directory so non-Steam
shortcuts display rich cover art. Game covers come from the bulk
post-sync phase (``POST_SYNC_PHASE_CHANGED``, gated behind
MetadataService so the three services don't hammer Steam's
``storesearch`` in parallel), which covers every owned game
whether installed or not. Concurrency capped via
``asyncio.Semaphore`` to stay under SGDB's rate limit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.cache_manager import CacheManager
from unifideck.event_bus.event_bus import EventBus
from unifideck.event_bus.event_bus_devex import auto_wire

from .event_handlers import _EventHandlersMixin
from .fetcher import download_and_save, get_missing_kinds
from .store_metadata import (
    fetch_store_urls,
    steam_cdn_urls,
    steam_search_appid,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

# Tuning knobs — overridable via config.
DEFAULT_MAX_CONCURRENT = 10
# Per-game artwork pipeline concurrency. Empirically tuned via
# tmp_test_sgdb_limits.py — SGDB tolerates 16+ concurrent
# autocomplete/grids calls without throttling (legacy "30/min"
# comment was stale; observed ~80 req/s sustained with zero 429s).
# 10 gives ~2.4× faster throughput than the old 4-wide cap while
# leaving headroom for the CDN image downloads that share the
# same semaphore. Cap via ``artwork.max_concurrent`` config key
# if your network needs a smaller batch.
DEFAULT_DOWNLOAD_TIMEOUT = 30
# seconds for the image download

# Hardcoded SGDB API key inherited from staging (``main.py:2125``).
# Without an explicit ``artwork.steamgriddb_api_key`` config
# override, we use this so first-run installs get covers
# automatically — the staging behaviour every existing user is
# already trained on. Users who want their own key (e.g. to
# avoid sharing rate-limit quota) can set the config field.
_STAGING_SGDB_API_KEY = "1a410cb7c288b8f21016c2df4c81df74"

# Five canonical Steam-grid artwork kinds + their filename
# suffixes. The unsigned 32-bit AppID is prepended at write
# time (Steam expects unsigned in shortcuts.vdf and on disk).
_ARTWORK_KINDS = ("grid", "grid_l", "hero", "logo", "icon")

# Cache namespace for the per-game *still-missing kind set* (sorted list).
# Lets a sync skip a game only when its missing kinds are unchanged since
# the last attempt — i.e. those kinds are genuinely unavailable upstream —
# instead of re-querying SGDB for them every sync. Ported from staging's
# ``artwork_attempts`` (main.py); reuses the slot already declared in
# ``bootstrap/cache_registry.py`` (ttl=0). Cleared on resync via
# _clear_resync_cache.
_ATTEMPTS_NAMESPACE = "artwork_attempts"


class ArtworkService(_EventHandlersMixin):
    """SGDB artwork fetcher wired to the EventBus."""

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        grid_dir: str,
        api_key: str | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        """Store collaborators, initialize configs and semaphores."""
        self._bus = bus
        self._cache = cache
        self._grid_dir = grid_dir
        self._config = config

        # API key resolution order:
        #   1. constructor arg (explicit injection — tests)
        #   2. user config key ``artwork.steamgriddb_api_key``
        #   3. bundled staging fallback (so first-run installs work)
        self._api_key = api_key
        if self._config and not self._api_key:
            self._api_key = self._config.get("artwork.steamgriddb_api_key", "")
        if not self._api_key:
            self._api_key = _STAGING_SGDB_API_KEY

        max_concurrent = DEFAULT_MAX_CONCURRENT
        self._download_timeout = DEFAULT_DOWNLOAD_TIMEOUT

        if self._config:
            max_concurrent = self._config.get("artwork.max_concurrent", DEFAULT_MAX_CONCURRENT)
            self._download_timeout = self._config.get("artwork.download_timeout", DEFAULT_DOWNLOAD_TIMEOUT)

        # Kept alongside the semaphore because the semaphore's own counter
        # drops as slots are taken; ``_batch_concurrency`` needs the
        # configured ceiling, not the momentary free count.
        self._max_concurrent = max(1, int(max_concurrent))
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

        # Track pending tasks so we can wait for them on shutdown
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        # The post-sync batch gather. Held so a user-initiated
        # ``SYNC_CANCELLED`` can cancel the whole batch (otherwise
        # the per-game ``progress.status == "cancelled"`` check
        # only short-circuits each task at its next checkpoint —
        # downloads already in flight keep going).
        self._batch_task: asyncio.Future[list[Any]] | None = None

        # We never run without an API key — the staging fallback
        # is bundled. Log the source so users can tell whether
        # they're on a custom key or the shared default.
        using_default = self._api_key == _STAGING_SGDB_API_KEY
        logger.info(
            "[ArtworkService] SteamGridDB API key configured (source: %s)",
            "shared default" if using_default else "user config",
        )

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` as if it were a bus
        # method, but ``auto_wire`` is module-level — the
        # call raised ``AttributeError`` and every
        # subscription was lost (caught and silenced upstream).
        auto_wire(self, self._bus)

    @property
    def grid_dir(self) -> str:
        return self._grid_dir

    def set_grid_dir(self, grid_dir: str) -> None:
        """Re-point at a different user's ``grid/`` dir at runtime.

        Driven by :func:`unifideck.steam.current_user.rebind_user_paths` when
        the active Steam user is (re)confirmed after boot, so artwork lands in
        the account the user is actually logged into.
        """
        self._grid_dir = grid_dir

    async def stop(self) -> None:
        """Wait for any in-flight downloads to complete, release the semaphore."""
        self._bus.unsubscribe_all(self)

        if self._pending_tasks:
            logger.info("[ArtworkService] waiting for %d pending downloads", len(self._pending_tasks))
            # Best-effort wait for in-flight tasks
            _, pending = await asyncio.wait(
                self._pending_tasks,
                timeout=5.0,
                return_when=asyncio.ALL_COMPLETED,
            )
            for t in pending:
                t.cancel()

    async def fetch_artwork(
        self,
        app_id: int,
        store: str,
        game_id: str,
        title: str,
        force: bool = False,
        extras: dict[str, Any] | None = None,
        only_kinds: set[str] | None = None,
    ) -> dict[str, bool]:
        """Three-source pipeline that mirrors staging.

        Sources in priority order :

        1. **Per-store API** — authoritative box-art from the
           store the game actually lives on (GOG/Amazon
           ``gamesdb.gog.com`` ``vertical_cover``, Epic
           Legendary cache, Ubisoft GraphQL extras). Skipped:
           GOG/Amazon logos (thumbnail-quality) and all icons
           (no store has good ones).
        2. **SteamGridDB** — curated community art, with
           dimension-filtered grid queries (portrait 600x900
           / 660x930, landscape 920x430 / 460x215). The shared
           staging API key is bundled so first-run works.
        3. **Steam Store CDN** — last resort for matched
           AppIDs, hitting ``shared.steamstatic.com`` for
           ``library_600x900_2x``, ``header.jpg``,
           ``library_hero.jpg``, ``logo.png``.

        ``only_kinds`` restricts the work to a subset of the five
        kinds — the caller passes the kinds actually missing on disk
        so a sync *backfills* gaps (logo / icon / landscape a previous
        sync missed) instead of re-downloading the whole set or, worse,
        treating the game as done the moment grid+hero land. ``None``
        means "compute the missing set from disk here".

        Returns ``{kind: bool}`` for all five Steam-grid kinds (kinds
        already present on disk report ``True`` without being
        re-fetched). The per-game *missing-set* cache skips a game only
        when its gaps are unchanged since the last attempt, so we don't
        re-query SGDB every sync for art that genuinely doesn't exist.
        """
        cache_key = f"{store}:{game_id}"
        target = await self._resolve_target_kinds(
            app_id, force, only_kinds,
        )

        # Kinds already on disk start True so the phases skip them and
        # we never re-download existing covers.
        result: dict[str, bool] = {k: (k not in target) for k in _ARTWORK_KINDS}
        if not target:
            return result

        if self._missing_set_unchanged(cache_key, target, force, title):
            logger.debug(
                "[ArtworkService] skipping %s: missing set unchanged (%s)",
                title, sorted(target),
            )
            return result

        current_task = asyncio.current_task()
        if current_task:
            self._pending_tasks.add(current_task)
            current_task.add_done_callback(self._pending_tasks.discard)

        async with self._semaphore:
            await self._run_fetch_phases(
                store, game_id, title, app_id, extras, cache_key, target, result,
            )
            return result

    async def _resolve_target_kinds(
        self,
        app_id: int,
        force: bool,
        only_kinds: set[str] | None,
    ) -> set[str]:
        """Resolve which artwork kinds still need fetching.

        Explicit ``only_kinds`` from the caller wins; ``force`` re-fetches
        everything; otherwise the gaps are read off disk.
        """
        if only_kinds is not None:
            return {k for k in only_kinds if k in _ARTWORK_KINDS}
        if force:
            return set(_ARTWORK_KINDS)
        return await get_missing_kinds(self._grid_dir, app_id)

    def _missing_set_unchanged(
        self,
        cache_key: str,
        target: set[str],
        force: bool,
        title: str,
    ) -> bool:
        """Whether the last attempt for this game asked the same question.

        Incremental skip: an identical missing set as last attempt means
        those kinds are genuinely unavailable upstream, so don't retry.
        ``force`` always re-fetches, so it never reports unchanged.

        **The title is part of the question.** Every source below the
        per-store phase — SGDB, the Steam CDN — matches on the title and
        nothing else, so the same missing set under a *different* title is a
        different query and its previous "nothing found" says nothing about
        it. Comparing only the kinds made a game whose title changed
        unfixable: it was skipped on every subsequent sync, forever, and the
        batch counter reported it as "no match" (see ``_fetch_one``), which
        reads as "SGDB has no art" rather than "we never asked".

        Found on a GameVault game the user's own server first exposed as
        ``Test Game 2026`` and later, once they enriched it, as ``Ghost of
        Tsushima``. GameVault is where this surfaces because the title comes
        from the user's server and changes when they curate it — but nothing
        about the bug is GameVault-specific: an Epic edition rename or a GOG
        re-release strands a game the same way.

        A record written before this fix carries no title. Treat that as
        changed rather than matching it: one extra query per affected game,
        once, and the cache is self-healing from then on.
        """
        if force:
            return False
        attempted = self._cache.get(_ATTEMPTS_NAMESPACE, cache_key)
        if not isinstance(attempted, dict):
            return False
        if attempted.get("title") != title:
            return False
        return set(attempted.get("missing") or ()) == target

    def _flush_artwork_caches(self) -> None:
        """Persist the batch's deferred attempts-cache writes."""
        try:
            self._cache.flush(_ATTEMPTS_NAMESPACE)
        except Exception:
            logger.debug(
                "[ArtworkService] cache flush %s failed", _ATTEMPTS_NAMESPACE,
            )

    async def _run_fetch_phases(
        self,
        store: str,
        game_id: str,
        title: str,
        app_id: int,
        extras: dict[str, Any] | None,
        cache_key: str,
        target: set[str],
        result: dict[str, bool],
    ) -> None:
        """Run the three-source fetch pipeline, mutating ``result`` in place.

        Phase 1 store metadata → Phase 2 SGDB fallback → Phase 3 Steam CDN,
        each filling only the kinds still missing. Records the residual
        missing set so the next sync can skip genuinely-absent art.
        """
        logger.info(
            "[ArtworkService] fetching art for %s (need: %s)",
            title, "+".join(sorted(target)),
        )
        sources: dict[str, str] = {}
        # Phase 1 — store metadata (authoritative).
        await self._fill_from_store(
            store, game_id, extras, app_id, result, sources,
        )
        # Phase 2 — SGDB fallback for any kind still missing.
        if not all(result.values()):
            await self._fill_from_sgdb(
                title, app_id, result, sources, only_kinds=target,
            )
        # Phase 3 — Steam CDN last resort.
        if not all(result.values()):
            await self._fill_from_steam_cdn(
                title, app_id, result, sources,
            )
        # Record what's still missing so the next sync can skip this
        # game iff the gaps haven't changed (genuinely-absent art).
        # Deferred write — one per game in the batch; the batch's
        # done-callback flushes via ``_flush_artwork_caches``.
        still_missing = sorted(k for k in _ARTWORK_KINDS if not result.get(k))
        # Title included: it is what SGDB and the Steam CDN were asked for,
        # so a later title makes this record inapplicable rather than final.
        self._cache.set(
            _ATTEMPTS_NAMESPACE,
            cache_key,
            {"missing": still_missing, "title": title},
            flush=False,
        )
        if sources:
            self._log_sources(title, sources)

    async def _fill_from_store(
        self,
        store: str,
        game_id: str,
        extras: dict[str, Any] | None,
        app_id: int,
        result: dict[str, bool],
        sources: dict[str, str],
    ) -> None:
        """Phase 1: pull authoritative URLs from the per-store API."""
        try:
            urls = await fetch_store_urls(store, game_id, extras)
        except Exception as e:
            logger.debug("[ArtworkService] store metadata failed: %s", e)
            return
        # Staging policy: skip stores' logo for GOG/Amazon (thumbnail
        # quality) and skip icon for every store (no clean icons).
        if store in ("gog", "amazon"):
            urls.pop("logo", None)
        urls.pop("icon", None)
        await self._download_kinds(
            urls, app_id, result, sources, label=store.upper(),
        )

    async def _fill_from_sgdb(
        self,
        title: str,
        app_id: int,
        result: dict[str, bool],
        sources: dict[str, str],
        only_kinds: set[str] | None = None,
    ) -> None:
        """Phase 2: batched SGDB lookup for everything still missing.

        Calls ``steamgriddb.fetch_all_kinds`` once per game — one
        title→game_id search followed by parallel asset fetches
        for the requested kinds. ``only_kinds`` narrows the asset
        fetches to the gaps we actually need (e.g. just ``icon``),
        sparing SGDB the kinds a store API already filled. Previous
        per-kind loop did 5 separate searches per game, blowing
        through the SGDB free-tier rate limit on large libraries.
        The package resolves ``grid_l`` natively with the
        landscape-dimension filter.
        """
        kinds = frozenset(only_kinds) if only_kinds else None
        try:
            from unifideck.steam import steamgriddb
            urls = await steamgriddb.fetch_all_kinds(
                title, self._api_key, config=self._config, only_kinds=kinds,
            )
        except Exception as e:
            # WARNING (was DEBUG): a blanket SGDB failure silently
            # stripped the whole library of community art + every icon.
            # Surface it so a TLS/DNS/rate-limit outage is greppable.
            logger.warning(
                "[ArtworkService] sgdb fetch failed (%s): %s: %s",
                title, type(e).__name__, e,
            )
            return
        for kind in _ARTWORK_KINDS:
            if result.get(kind):
                continue
            url = urls.get(kind)
            if not url:
                continue
            ok = await download_and_save(
                self._grid_dir, app_id, kind, url, self._download_timeout,
            )
            if ok:
                result[kind] = True
                sources[kind] = "SGDB"

    async def _fill_from_steam_cdn(
        self,
        title: str,
        app_id: int,
        result: dict[str, bool],
        sources: dict[str, str],
    ) -> None:
        """Phase 3: Steam Store CDN for any kind still missing.

        Resolves the title to a real Steam AppID (cached by
        ``MetadataService.fetch_appdetails_for_game`` when
        available, otherwise live-searched here), then pulls
        the canonical ``shared.steamstatic.com`` URLs from
        :func:`steam_cdn_urls`.
        """
        steam_id = self._lookup_cached_steam_id(app_id)
        if steam_id is None:
            try:
                steam_id = await steam_search_appid(title)
            except Exception:
                steam_id = None
        if not steam_id:
            return
        urls = steam_cdn_urls(steam_id)
        await self._download_kinds(
            urls, app_id, result, sources, label="STEAM",
        )

    def _lookup_cached_steam_id(self, app_id: int) -> int | None:
        """Read the precomputed shortcut-AppID → Steam-AppID mapping."""
        try:
            stores = getattr(self._cache, "_stores", None)
            if not isinstance(stores, dict):
                return None
            data = getattr(stores.get("steam_real_appid"), "_data", None)
            if not isinstance(data, dict):
                return None
            value = data.get(str(app_id))
            return value if isinstance(value, int) and value > 0 else None
        except Exception:
            return None

    async def _download_kinds(
        self,
        urls: dict[str, str],
        app_id: int,
        result: dict[str, bool],
        sources: dict[str, str],
        *,
        label: str,
    ) -> None:
        """Download every URL in ``urls`` that fills a missing kind.

        Mutates ``result`` and ``sources`` in place.  Each download
        is awaited sequentially to stay polite with the upstream
        CDN; the outer semaphore already caps cross-game
        parallelism.
        """
        for kind, url in urls.items():
            if kind not in _ARTWORK_KINDS or result.get(kind):
                continue
            if not url:
                continue
            ok = await download_and_save(
                self._grid_dir, app_id, kind, url, self._download_timeout,
            )
            if ok:
                result[kind] = True
                sources[kind] = label

    def _log_sources(self, title: str, sources: dict[str, str]) -> None:
        """Emit a single-line summary of where each kind came from."""
        by_source: dict[str, list[str]] = {}
        for kind, src in sources.items():
            by_source.setdefault(src, []).append(kind)
        summary = " ".join(
            f"{src}:{'+'.join(sorted(kinds))}"
            for src, kinds in by_source.items()
        )
        logger.info("[ArtworkService] %s → %s", title, summary)
