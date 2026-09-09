"""services/metadata_service.py — Game metadata resolver.

EventBus subscriber enriching ``Game`` objects with metadata
from 3 sources in priority order:
1. Steam Store — matches non-Steam games to their Steam app_id
   when one exists (real description, images, genres).
2. UnifiDB — Unifideck's own game database (niche + non-Steam).
3. Metacritic — scores and review summaries.

All responses cached (CacheManager) with a 7-day TTL to avoid
hammering third-party APIs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from unifideck.core.sync_generation import run_id_of
from unifideck.core.types import Game
from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.services import metadata_backfill, metadata_sources, pcgw_backfill
from unifideck.services.metadata_completeness import has_complete_metadata
from unifideck.services.metadata_steam_mixin import (
    CACHE_NAMESPACE,
    _SteamMetadataMixin,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


# Schema stamp for the unifiDB save-location block on a cached entry. Entries
# without the current value are topped up in place by
# ``_served_from_cache`` — see the note there. Bump only when the shape of
# ``save_locations``/``cloud``/``save_source`` changes in a way that needs a
# re-fetch; it does NOT invalidate the (expensive) Steam half of the entry.
_SAVEDATA_SCHEMA_KEY = "_savedata_v"
# v2: the unifiDB bucket lookup stripped leading articles while the catalog
# buckets by the raw title, so every "The …" / "A …" title missed its shard
# entirely and got stamped v1 with no save data (The Witcher 3 among them).
# The bump makes those entries re-query now that the shard is right.
_SAVEDATA_SCHEMA = 2
DEFAULT_CACHE_TTL = 7 * 24 * 3600  # fallback if config missing

# Per-game concurrency cap. Steam's ``appdetails`` rate limit is the
# binding constraint (UnifiDB / Metacritic are unconstrained on our
# side); the shared ``STEAM_STORE_GATE`` in ``steam/http_retry.py``
# pauses every worker on a 429, so overshoot degrades into a brief
# collective pause instead of a retry storm. Overridable via the
# ``metadata.max_concurrent`` config key for field tuning.
ENRICHMENT_CONCURRENCY = 10


def _cancel_pending(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel every task in ``tasks`` that hasn't finished yet."""
    for t in tasks:
        if not t.done():
            t.cancel()


class MetadataService(_SteamMetadataMixin):
    """Enriches Game objects with cross-store metadata.

    The Steam-Store tail (AppID resolution, appdetails, reviews,
    Date-Added stamp) lives in :class:`_SteamMetadataMixin`
    (``metadata_steam_mixin.py``) — split out for the volumetry cap.
    """

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        config: ConfigManager | None = None,
    ) -> None:
        """Store refs, read config, auto_wire."""
        self._bus = bus
        self._cache = cache
        self._config = config
        # Background enrichment task. Held so that a new
        # SYNC_COMPLETE can cancel the prior run before
        # starting its own — otherwise overlapping tasks both
        # increment ``SyncProgress.*_synced`` against the same
        # tracker, producing inflated numerators (the "1089/563"
        # symptom).
        self._enrichment_task: asyncio.Task[None] | None = None

        # NOTE: the cache TTL is owned by the registry, not the
        # service — see the ``"metadata"`` entry in
        # ``bootstrap/cache_registry.py``. ``metadata.cache_ttl``
        # in user config is currently unused; if per-user TTL
        # tuning is wanted, the right place to read it is at
        # ``register_default_caches`` time, not here.

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` guarded by
        # ``hasattr`` — but ``auto_wire`` is module-level,
        # not a bus method, so the hasattr check returned
        # False and every subscription was silently dropped.
        auto_wire(self, self._bus)

    async def stop(self) -> None:
        """Lifecycle hook — currently a no-op."""

    @subscribe(Events.SYNC_CANCELLED)
    async def _on_sync_cancelled(self, **_kwargs: Any) -> None:
        """Cancel any in-flight metadata enrichment immediately.

        User-initiated cancel must stop the per-game enrichment
        loop, not just the per-store fetch — otherwise the bar
        disappears but the 5-15 minutes of HTTP work keeps
        running in the background, ticking ``SyncProgress``
        counters that the user thought were dead.
        """
        task = self._enrichment_task
        if task is not None and not task.done():
            task.cancel()

    @subscribe(Events.SYNC_COMPLETE)
    async def _on_sync_complete(self, **kwargs: Any) -> None:
        """Schedule enrichment as a background task and return immediately.

        Critical: the enrichment loop hits 3 HTTP APIs per game and
        sequentially-paces the Steam ``appdetails`` fetch (~0.25s
        per non-Steam game) — for 500+ games that's 5-15 minutes
        of work. Awaiting it inside this handler would block
        :meth:`asyncio.gather` in ``bus.emit(SYNC_COMPLETE, ...)``,
        which in turn blocks :meth:`SyncService._finalize_sync`,
        which holds ``SyncService._lock`` the entire time. The
        net effect on the user: "sync_all called while another
        sync is running — rejected" for the next 10+ minutes, and
        the frontend's ``await startMut.mutate()`` never resolves
        so the cooldown timer never starts.

        Solution: spawn the loop as a fire-and-forget task. The
        ``SYNC_COMPLETE`` emit returns immediately, the sync lock
        releases, the frontend gets its RPC response, and the
        enrichment quietly progresses in the background.
        """
        games = kwargs.get("games", [])
        # Cancel any prior enrichment still running. Two syncs
        # back-to-back (or a sync that was cancelled mid-enrich)
        # would otherwise leave the old task ticking
        # ``SyncProgress.*_synced`` on the same tracker the new
        # run just reset to 0 — the user sees a numerator larger
        # than the library size (e.g. "1089 / 563" with 563
        # games actually synced). Cancel is fire-and-forget: we
        # must NOT await it here (this handler runs inside
        # ``bus.emit(SYNC_COMPLETE)`` which is awaited by
        # ``SyncService._finalize_sync`` while holding the sync
        # lock; awaiting would re-introduce the multi-minute
        # lock-up that the fire-and-forget pattern fixed).
        prior = self._enrichment_task
        if prior is not None and not prior.done():
            prior.cancel()
        # Schedule unconditionally — even with games=[] the task
        # must run so its ``finally`` clause fires the phase-done
        # event. SyncService gates ``mark_complete`` on receiving
        # one POST_SYNC_PHASE_CHANGED per pending phase; a missing
        # signal strands ``_post_sync_pending`` and the progress
        # bar never reaches 100%.
        #
        # Stash the SYNC_COMPLETE kwargs so the phase-done emit can
        # forward them to downstream services (ArtworkService /
        # CompatibilityService) which now wait on this phase
        # instead of subscribing to SYNC_COMPLETE directly. This
        # serialises three previously-parallel pipelines that
        # were colliding on Steam's storesearch rate-limit.
        self._sync_kwargs = dict(kwargs)
        is_force = bool(kwargs.get("is_force"))
        self._enrichment_task = asyncio.create_task(
            self._run_enrichment(
                games, is_force=is_force,
                skip=bool(kwargs.get("skip_chain")),
            ),
            name="metadata-enrichment",
        )

    async def _run_enrichment(
        self, games: list[Game], *, is_force: bool = False,
        skip: bool = False,
    ) -> None:
        """Background enrichment loop. ``finally`` emits
        ``POST_SYNC_PHASE_CHANGED(active=False)`` so the sync's
        post-phase tracker advances on success, exception, or
        user-initiated sync cancellation.

        ``is_force`` (force sync) skips the already-cached partition
        and re-fetches every game — cache entries are bypassed on
        read and overwritten on completion.

        ``skip`` is SyncService's "this run covers exactly what the last
        completed chain covered" signal. The loop still runs (so the
        ``finally`` announces the phase and the chain hands off to
        Artwork), it just does no work.
        """
        total = len(games)
        cancelled_by_replace = False
        try:
            if not games or skip:
                if skip:
                    logger.info(
                        "[MetadataService] enrichment skipped — library "
                        "unchanged since the last completed chain",
                    )
                return
            progress = self._sync_progress()
            if progress is not None:
                progress.start_metadata(total)
            logger.info(
                "[MetadataService] background enrichment started "
                "for %d games (force=%s)",
                total, is_force,
            )
            complete_games, pending_games = (
                ([], list(games)) if is_force
                else self._partition_games(games)
            )
            await self._mark_complete_cached(complete_games, progress, total)
            if pending_games:
                await self._enrich_pending(
                    pending_games, progress, total, len(complete_games),
                    force=is_force,
                )
        except asyncio.CancelledError:
            cancelled_by_replace = True
            logger.info(
                "[MetadataService] enrichment cancelled — newer sync took over",
            )
            raise
        finally:
            await self._finalize_enrichment(cancelled_by_replace, total, games)

    def _partition_games(
        self, games: list[Game],
    ) -> tuple[list[Game], list[Game]]:
        """Split games into ``(already-complete, pending-enrichment)``."""
        complete_games: list[Game] = []
        pending_games: list[Game] = []
        for g in games:
            if has_complete_metadata(self._cache, g):
                complete_games.append(g)
            else:
                pending_games.append(g)
        return complete_games, pending_games

    async def _mark_complete_cached(
        self, complete_games: list[Game], progress: Any, total: int,
    ) -> None:
        """Instantly advance progress for games already fully cached."""
        if not complete_games:
            return
        logger.info(
            "[MetadataService] %d/%d games already have complete metadata cached",
            len(complete_games), total,
        )
        if progress is None:
            return
        for g in complete_games:
            await progress.increment_steam(g.title)
            await progress.increment_unifidb(g.title)

    async def _enrich_pending(
        self,
        pending_games: list[Game],
        progress: Any,
        total: int,
        complete_count: int,
        *,
        force: bool = False,
    ) -> None:
        """Run concurrent enrichment for the games that are missing data."""
        logger.info(
            "[MetadataService] Enqueueing %d games missing metadata for enrichment",
            len(pending_games),
        )
        sem = asyncio.Semaphore(self._max_concurrent())
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                asyncio.create_task(
                    self._enrich_one_game(g, sem, session=session, force=force),
                )
                for g in pending_games
            ]
            await self._drain_enrichment(
                tasks, progress, total, start_count=complete_count,
            )

    async def _finalize_enrichment(
        self, cancelled_by_replace: bool, total: int, games: list[Game],
    ) -> None:
        """``finally`` body: emit the phase-done event and spawn long-tail
        backfills (skipped when a newer sync cancelled this run)."""
        # Persist the loop's deferred cache writes before announcing
        # the phase done (cancelled runs flush too — partial data is
        # still valid data).
        self._flush_deferred_caches()
        if not cancelled_by_replace:
            # Forward SYNC_COMPLETE kwargs so the serialised
            # Artwork → Compat downstream chain reads them here.
            sync_kwargs = getattr(self, "_sync_kwargs", None) or {}
            await self._bus.emit(
                Events.POST_SYNC_PHASE_CHANGED,
                phase="metadata", active=False,
                total=total, done=total,
                sync_kwargs=sync_kwargs,
                # Echo the generation so a late phase-done cannot drain a
                # newer run's pending set (``core/sync_generation.py``).
                run_id=run_id_of(sync_kwargs),
            )
        logger.info(
            "[MetadataService] background enrichment finished (%d games)",
            total,
        )
        # Long-tail Metacritic + PCGamingWiki lookups: fire-and-forget.
        if not cancelled_by_replace:
            metadata_backfill.spawn(self, games)
            pcgw_backfill.spawn(self, games)

    def _sync_progress(self) -> Any:
        """Return the bus's ``SyncProgress`` tracker, or ``None``."""
        if not hasattr(self._bus, "get_sync_progress"):
            return None
        return self._bus.get_sync_progress()

    def _max_concurrent(self) -> int:
        """Read ``metadata.max_concurrent`` from config or fall back.

        Mirrors ``CompatibilityService._max_concurrent`` — a config
        knob so field devices hitting 429s can drop back below the
        default without a plugin update.
        """
        if self._config is None:
            return ENRICHMENT_CONCURRENCY
        try:
            value = self._config.get(
                "metadata.max_concurrent", ENRICHMENT_CONCURRENCY,
            )
            return max(1, int(value))
        except Exception:
            return ENRICHMENT_CONCURRENCY

    async def _drain_enrichment(
        self,
        tasks: list[asyncio.Task[None]],
        progress: Any,
        total: int,
        start_count: int = 0,
    ) -> None:
        """Await every per-game task as it finishes, logging progress."""
        every = max(1, min(50, total // 5))
        done_count = start_count
        for fut in asyncio.as_completed(tasks):
            if progress is not None and progress.status == "cancelled":
                logger.info(
                    "[MetadataService] cancel detected at %d/%d — aborting",
                    done_count, total,
                )
                _cancel_pending(tasks)
                break
            try:
                await fut
            except Exception:
                logger.debug(
                    "[MetadataService] enrichment task raised", exc_info=True,
                )
            done_count += 1
            if done_count % every == 0:
                logger.info(
                    "[MetadataService] progress: %d/%d enriched",
                    done_count, total,
                )

    async def _enrich_one_game(
        self,
        game: Game,
        sem: asyncio.Semaphore,
        session: aiohttp.ClientSession | None = None,
        *,
        force: bool = False,
    ) -> None:
        """Per-game enrichment under the semaphore: enrich → appdetails → progress."""
        async with sem:
            steam_id: int | None = None
            try:
                enriched = await self.enrich(game, session=session, force=force)
                raw = enriched.get("steam_appid")
                if isinstance(raw, int) and raw > 0:
                    steam_id = raw
            except Exception as e:
                logger.warning(
                    "[MetadataService] enrichment failed for %s: %s",
                    game.title, e,
                )
            if game.store != "steam":
                try:
                    await self.fetch_appdetails_for_game(
                        game, hint_steam_id=steam_id, session=session,
                        force=force,
                    )
                except Exception as e:
                    logger.debug(
                        "[MetadataService] appdetails failed for %s: %s",
                        game.title, e,
                    )
            progress = self._sync_progress()
            if progress is not None:
                await progress.increment_steam(game.title)
                await progress.increment_unifidb(game.title)

    async def _served_from_cache(
        self, game: Game, cached: dict[str, Any], cache_key: str,
    ) -> dict[str, Any]:
        """Return a cached entry, topping up the unifiDB half if it predates it.

        ``fetch_unifidb`` used to drop the save-location block
        (``save_locations`` / ``cloud`` / ``save_source``) before it reached
        this cache. With a 30-day TTL, simply fixing that left every existing
        user's entries save-data-less for up to a month — a normal library
        sync reads the cache and returns early, so the cloud-save button, the
        save-path resolver and the pre-install cloud indicator would all stay
        broken with no signal to the user that a *force* sync was needed.

        So entries written before the fix are stamped-checked and topped up
        in place. Only the unifiDB source re-runs: its bucket files are
        themselves cached (``unifidb_metadata``, 30 d), so the whole library
        costs ~36 CDN reads rather than 1200 Steam API calls, and the Steam
        half of the entry is left untouched.
        """
        if cached.get(_SAVEDATA_SCHEMA_KEY) == _SAVEDATA_SCHEMA:
            return cached
        try:
            fresh = await metadata_sources.fetch_unifidb(game, config=self._config)
        except Exception as e:  # pragma: no cover - best-effort top-up
            logger.debug("[MetadataService] save-data top-up failed for %s: %s",
                         cache_key, e)
            return cached
        topped = dict(cached)
        for field in ("save_locations", "cloud", "save_source"):
            value = fresh.get(field)
            if value:
                topped[field] = value
        # Stamp regardless of whether the catalog had anything, so a game with
        # genuinely no save data is not re-queried on every single enrich().
        topped[_SAVEDATA_SCHEMA_KEY] = _SAVEDATA_SCHEMA
        try:
            self._cache.set(CACHE_NAMESPACE, cache_key, topped, flush=False)
        except Exception as e:  # pragma: no cover - cache is best-effort
            logger.debug("[MetadataService] save-data top-up write failed: %s", e)
        return topped

    async def enrich(
        self,
        game: Game,
        session: aiohttp.ClientSession | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Return enriched metadata for a single game.

        ``force=True`` bypasses the cache read (negative markers
        included) and overwrites the entry with the fresh merge.
        """
        cache_key = f"{game.store}:{game.store_game_id}"

        if not force:
            try:
                cached = self._cache.get(CACHE_NAMESPACE, cache_key)
                if isinstance(cached, dict):
                    if cached.get("_negative"):
                        return {}
                    if cached:
                        return await self._served_from_cache(game, cached, cache_key)
            except Exception as e:
                logger.debug("[MetadataService] Cache read failed for %s: %s", cache_key, e)

        # Cache miss — fetch
        logger.debug("[MetadataService] Fetching metadata for %s", game.title)

        results = await asyncio.gather(
            metadata_sources.fetch_steam_store(
                game.title, config=self._config, session=session,
            ),
            metadata_sources.fetch_unifidb(game, config=self._config),
            return_exceptions=True,
        )

        steam_data = results[0] if isinstance(results[0], dict) else {}
        unifidb_data = results[1] if isinstance(results[1], dict) else {}

        # Merge (Steam > UnifiDB)
        merged: dict[str, Any] = {}
        merged.update(unifidb_data)
        merged.update(steam_data)

        try:
            payload = merged if merged else {"_negative": True}
            # Deferred write — the enrichment loop calls this once per
            # game; ``_finalize_enrichment`` flushes at the phase end.
            self._cache.set(CACHE_NAMESPACE, cache_key, payload, flush=False)
        except Exception as e:
            logger.warning("[MetadataService] Failed to cache metadata for %s: %s", cache_key, e)

        return merged

