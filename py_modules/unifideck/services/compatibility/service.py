"""CompatibilityService — post-sync ProtonDB + Deck-Verified fetcher.

Subscribes to ``SYNC_COMPLETE`` and walks the game list, resolving
each title to its compat rating via :class:`CompatLibrary`. Mirrors
the pattern of :mod:`unifideck.services.metadata_service` (fire-and-
forget background task, ``POST_SYNC_PHASE_CHANGED`` on completion,
tick-per-game progress, cancel-checkpoint between iterations).

Why this is its own service
===========================
* The compat fetch is HTTP-heavy (~50ms per title on a good day,
  longer when ProtonDB is grumpy). Coupling it to MetadataService
  would mean a single failure window for two unrelated data sources.
* Compat ratings update independently of metadata (a tier change on
  ProtonDB doesn't invalidate the Steam Store payload), so a
  separate cache namespace + lifecycle is cleaner.
* The phase has its own progress band (95-98) on the UI, so the user
  sees what's happening — the staging behaviour every user is
  trained on.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from unifideck.compatibility import CompatLibrary
from unifideck.compatibility.library import needs_refetch
from unifideck.core.sync_generation import UNTAGGED_RUN_ID, run_id_of
from unifideck.core.types import Game
from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.core.sync_service import SyncService
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

# Per-game concurrency cap for the compat fetch loop. Empirically
# tuned via tmp_test_compat_limits.py — ProtonDB + Steam's
# saleaction endpoint both tolerate 16+ concurrent calls without
# throttling. 10 gives ~7× speedup over the old sequential+50ms
# pacing (8 min → ~1 min on a 1130-game library) with comfortable
# headroom. Overridable via ``compat.max_concurrent`` config.
DEFAULT_MAX_CONCURRENT = 10


class CompatibilityService:
    """Resolves ProtonDB / Deck-Verified ratings after each sync."""

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        sync_service: SyncService | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        """Store collaborators + register the phase + auto-wire handlers.

        ``sync_service`` is optional so the service can be constructed
        in test contexts without the full bootstrap, but registering
        the ``proton_meta`` phase is what makes ``mark_complete``
        wait for our done-event — without it the progress bar races
        to 100% before we've ticked.
        """
        self._bus = bus
        self._cache = cache
        self._config = config
        # Deferred writes: the per-sync loop writes once per game;
        # ``_run_enrichment``'s ``finally`` flushes both namespaces.
        self._lib = CompatLibrary(
            cache=cache, config=config, deferred_writes=True,
        )
        self._enrichment_task: asyncio.Task[None] | None = None
        if sync_service is not None:
            sync_service.register_post_sync_phase("proton_meta")
        auto_wire(self, self._bus)

    async def stop(self) -> None:
        """Lifecycle hook — let any in-flight enrichment task finish."""
        if self._enrichment_task is not None and not self._enrichment_task.done():
            try:
                await asyncio.wait_for(self._enrichment_task, timeout=5.0)
            except (TimeoutError, Exception):
                self._enrichment_task.cancel()

    def wire_sync_service(self, sync_service: SyncService) -> None:
        """Post-construction injection of the SyncService reference.

        SyncService and CompatibilityService are built in separate
        bootstrap layers (4 and 5 respectively). The constructor
        accepts ``sync_service=None`` so it can be built without
        knowing about the future SyncService instance; this setter
        is called after Layer 5 finishes, registering the
        ``proton_meta`` phase so ``mark_complete`` waits for our
        done-event.
        """
        sync_service.register_post_sync_phase("proton_meta")

    @subscribe(Events.SYNC_CANCELLED)
    async def _on_sync_cancelled(self, **_kwargs: Any) -> None:
        """Cancel the in-flight ProtonDB lookup loop on user cancel."""
        task = self._enrichment_task
        if task is not None and not task.done():
            task.cancel()

    @subscribe(Events.POST_SYNC_PHASE_CHANGED)
    async def _on_artwork_phase_done(self, **kwargs: Any) -> None:
        """Schedule background compat enrichment after Artwork finishes.

        Previously subscribed directly to ``SYNC_COMPLETE`` and
        raced ArtworkService + MetadataService for Steam's
        ``storesearch`` endpoint. Switching to wait for Artwork's
        phase-done event serialises the chain
        Metadata → Artwork → Compat, so by the time we start the
        ``steam_real_appid`` cache is fully populated and every
        ProtonDB lookup can short-circuit the ``search_store`` call.

        Fires only on the precise ``phase="artwork", active=False``
        flank to avoid reacting to every phase emit on the bus.
        Falls back to ``kwargs`` directly if ``sync_kwargs`` isn't
        present (defensive — supports older emitters that haven't
        been migrated yet).
        """
        if kwargs.get("phase") != "artwork":
            return
        if kwargs.get("active") is not False:
            return
        # ``sync_kwargs`` is the only shape any emitter has ever sent. The
        # flat ``kwargs.get("games")`` / ``kwargs.get("is_force")`` fallback
        # that used to sit here was for "older emitters that haven't been
        # migrated yet" — there were none, in the whole history of the file.
        # Audit register item 41; found by the new subscribe-side arm of
        # validate_event_schemas.py.
        sync_kwargs = kwargs.get("sync_kwargs") or {}
        games = sync_kwargs.get("games") or []
        is_force = bool(sync_kwargs.get("is_force"))
        prior = self._enrichment_task
        if prior is not None and not prior.done():
            prior.cancel()
        self._enrichment_task = asyncio.create_task(
            self._run_enrichment(
                games, is_force=is_force,
                run_id=run_id_of(sync_kwargs),
                skip=bool(sync_kwargs.get("skip_chain")),
            ),
            name="compatibility-enrichment",
        )

    async def _run_enrichment(
        self, games: list[Game], *, is_force: bool = False,
        run_id: int = UNTAGGED_RUN_ID, skip: bool = False,
    ) -> None:
        """Per-game ProtonDB + Deck-Verified lookup, concurrent under a semaphore.

        Standard sync partitions out games whose rating is already
        cached (mirrors ``MetadataService._partition_games``) — they
        tick the progress counter instantly and cost zero HTTP.
        ``is_force`` skips the partition and refreshes every entry.

        ``run_id`` is echoed on the phase-done event so a late emit cannot
        drain a newer sync's pending set (``core/sync_generation.py``).
        """
        total = len(games)
        progress = self._bus.get_sync_progress() if hasattr(self._bus, "get_sync_progress") else None
        # Set when a newer sync's ``_on_artwork_phase_done`` cancelled this
        # run. The old unconditional emit in ``finally`` announced
        # ``proton_meta`` done on behalf of a run that had been replaced,
        # dropping the phase from the *new* generation's pending set before
        # its own compat pass had started. MetadataService already drew this
        # distinction; this is the same guard. It also avoids awaiting
        # ``bus.emit`` while a CancelledError is propagating.
        cancelled_by_replace = False
        try:
            if not games or skip:
                if skip:
                    logger.info(
                        "[CompatibilityService] compat skipped — library "
                        "unchanged since the last completed chain",
                    )
                return
            if progress is not None:
                progress.start_compat(total)
            skipped, pending = (
                ([], list(games)) if is_force
                else self._partition_games(games)
            )
            logger.info(
                "[CompatibilityService] compat fetch started for %d games "
                "(%d skipped (cached), %d pending, force=%s)",
                total, len(skipped), len(pending), is_force,
            )
            await self._tick_skipped(skipped, progress)
            if pending:
                await self._fetch_pending(
                    pending, progress, total, refresh=is_force,
                )
        except asyncio.CancelledError:
            cancelled_by_replace = True
            logger.info(
                "[CompatibilityService] compat fetch cancelled — "
                "newer sync took over",
            )
            raise
        finally:
            # Partial ratings are still valid ratings, so a cancelled run
            # flushes what it resolved before bowing out.
            self._flush_compat_caches()
            if not cancelled_by_replace:
                await self._bus.emit(
                    Events.POST_SYNC_PHASE_CHANGED,
                    phase="proton_meta", active=False,
                    total=total, done=total,
                    run_id=run_id,
                )
                logger.info(
                    "[CompatibilityService] compat fetch finished (%d games)",
                    total,
                )

    async def repair_missing(self, games: list[Game]) -> int:
        """Resolve compat ratings for ``games``, outside the sync chain.

        Used by :class:`~unifideck.services.post_sync_reconcile.
        PostSyncReconcileService` to close the tail an interrupted sync
        left behind. Deliberately does **not** touch ``SyncProgress`` or
        emit ``POST_SYNC_PHASE_CHANGED``: those belong to a sync run, and
        a phase event emitted at boot would drain a pending set that no
        run owns — which would mark a chain complete that never happened.

        Args:
            games: the subset already known to be missing a rating.

        Returns:
            How many games were attempted.
        """
        if not games:
            return 0
        sem = asyncio.Semaphore(self._max_concurrent())
        try:
            async with aiohttp.ClientSession() as session:
                await asyncio.gather(
                    *(
                        self._fetch_one(
                            g, sem, None, refresh=False, session=session,
                        )
                        for g in games
                    ),
                    return_exceptions=True,
                )
        finally:
            self._flush_compat_caches()
        return len(games)

    @staticmethod
    async def _tick_skipped(skipped: list[Game], progress: Any | None) -> None:
        """Advance the progress counter instantly for cached games."""
        if progress is None:
            return
        for g in skipped:
            await progress.increment_compat(g.title)

    async def _fetch_pending(
        self,
        pending: list[Game],
        progress: Any | None,
        total: int,
        *,
        refresh: bool,
    ) -> None:
        """Fan out the per-game lookups under one shared session.

        One session per run (``ssl=False`` — the permissive-TLS
        invariant): per-call sessions cost two TLS handshakes per
        game on a cold sync.
        """
        sem = asyncio.Semaphore(self._max_concurrent())
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as sess:
            tasks = [
                asyncio.create_task(
                    self._fetch_one(
                        g, sem, progress, refresh=refresh, session=sess,
                    ),
                )
                for g in pending
            ]
            await self._drain(tasks, progress, total)

    def _flush_compat_caches(self) -> None:
        """Persist the loop's deferred compat/appid-mapping writes."""
        for namespace in ("compat", "steam_real_appid"):
            try:
                self._cache.flush(namespace)
            except Exception:
                logger.debug(
                    "[CompatibilityService] cache flush %s failed", namespace,
                )

    def _partition_games(
        self, games: list[Game],
    ) -> tuple[list[Game], list[Game]]:
        """Split games into ``(already-cached, pending-fetch)``.

        Mirrors ``MetadataService._partition_games``. Before this
        partition existed the compat phase visited every game every
        sync: titles that never resolve on Steam re-ran
        ``search_store`` forever, and entries with no published
        test results re-hit the Deck-Verified endpoint forever.
        """
        skipped: list[Game] = []
        pending: list[Game] = []
        for g in games:
            (skipped if self._has_cached_compat(g) else pending).append(g)
        return skipped, pending

    def _has_cached_compat(self, game: Game) -> bool:
        """True when this sync could fetch nothing new for ``game``."""
        mapping = self._lib.cached_steam_mapping(game.app_id)
        if mapping is None:
            # Never resolved — worth an attempt (backfills the
            # mapping the metadata phase missed).
            return False
        if mapping <= 0:
            # Negative-cached: no Steam counterpart exists. Only a
            # force sync (via the metadata phase's re-resolution)
            # retries these.
            return True
        entry = self._cached_compat_entry(mapping)
        if entry is None:
            return False
        return not self._needs_self_heal(entry)

    def _cached_compat_entry(self, steam_id: int) -> dict[str, Any] | None:
        """Read the ``compat`` cache entry for a real Steam AppID."""
        try:
            value = self._cache.get("compat", str(steam_id))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _needs_self_heal(entry: dict[str, Any]) -> bool:
        """Cache entries below the current schema get ONE upgrade fetch.

        The schema stamp (written by ``CompatLibrary``) marks the
        upgrade as attempted, so a title with genuinely no published
        rating stops re-fetching every sync. This is the twin of
        ``CompatLibrary.needs_refetch`` and delegates to it — the two
        deciding differently would mean re-fetching forever or never.
        """
        return needs_refetch(entry)

    async def _fetch_one(
        self,
        game: Game,
        sem: asyncio.Semaphore,
        progress: Any | None,
        *,
        refresh: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Per-game lookup body — under the semaphore.

        ``increment_compat`` runs unconditionally so the UI counter
        ticks even when the upstream call raises (failure → "we
        attempted this game", not a stall).
        """
        async with sem:
            try:
                # Pass the shortcut AppID so CompatLibrary can reuse
                # the ``steam_real_appid`` cache populated by
                # MetadataService — skips a per-game storesearch.
                await self._lib.get_for_title(
                    game.title, shortcut_app_id=game.app_id,
                    refresh=refresh, session=session,
                )
            except Exception as e:
                logger.debug(
                    "[CompatibilityService] compat fetch failed for %s: %s",
                    game.title, e,
                )
            if progress is not None:
                await progress.increment_compat(game.title)

    async def _drain(
        self, tasks: list[asyncio.Task[None]], progress: Any | None, total: int,
    ) -> None:
        """Await tasks as they finish; honour the cancel-status flank."""
        for done_count, fut in enumerate(asyncio.as_completed(tasks)):
            if progress is not None and progress.status == "cancelled":
                logger.info(
                    "[CompatibilityService] cancel detected at %d/%d — aborting",
                    done_count, total,
                )
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break
            try:
                await fut
            except Exception:
                logger.debug(
                    "[CompatibilityService] drained task raised", exc_info=True,
                )

    def _max_concurrent(self) -> int:
        """Read ``compat.max_concurrent`` from config or fall back to default."""
        if self._config is None:
            return DEFAULT_MAX_CONCURRENT
        try:
            value = self._config.get(
                "compat.max_concurrent", DEFAULT_MAX_CONCURRENT,
            )
        except Exception:
            return DEFAULT_MAX_CONCURRENT
        try:
            n = int(value)
        except (TypeError, ValueError):
            return DEFAULT_MAX_CONCURRENT
        return max(1, n)
