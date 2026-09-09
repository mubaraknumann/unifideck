"""services/artwork/event_handlers.py — EventBus subscribers.

``@subscribe``-decorated handlers driving the artwork pipeline.
All ultimately call ``self.fetch_artwork`` on the host; they
differ in trigger signals and payload shapes.

Which handler covers what:

* ``POST_SYNC_PHASE_CHANGED`` — the bulk pass, and the one that covers game
  shortcuts. See the note above ``_on_artwork_request`` for why there is no
  install-time handler.
* ``SHORTCUT_CREATED`` — auth tiles, emitted by
  ``stores/shared/auth_shortcut.ensure_auth_shortcut`` when it writes a new
  persistent shortcut. Battle.net is its only live source; Ubisoft fetches
  its own auth artwork and the four OAuth stores use ephemeral shortcuts.
* ``ARTWORK_REQUEST`` — on-demand force-refetch. No emitter yet; see the
  ``unwired:`` note on the enum member.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.sync_generation import run_id_of
from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import subscribe

if TYPE_CHECKING:
    from unifideck.core.types import Game
    # This is a mixin; `self` will be the ArtworkService facade at runtime.

logger = logging.getLogger(__name__)

# Strong references to background fetch tasks so the GC can't
# collect them mid-flight (see RUF006). Tasks remove themselves on
# completion via ``add_done_callback``.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    """Register a fire-and-forget task so the GC doesn't collect it early."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _sync_progress(bus: Any) -> Any:
    """Return the shared ``SyncProgress`` tracker off *bus*, or ``None``."""
    if bus is None or not hasattr(bus, "get_sync_progress"):
        return None
    return bus.get_sync_progress()


def _batch_results(future: asyncio.Future[list[Any]]) -> list[Any] | None:
    """The gather's results, or ``None`` when the batch was cancelled.

    A cancelled batch shows up in two states and only one answers ``True``
    to ``future.cancelled()``: cancelling a ``gather(...,
    return_exceptions=True)`` usually leaves the gathering future *finished*
    carrying a ``CancelledError``. The old ``if future.cancelled(): return``
    guard therefore fell through and ``future.result()`` raised inside the
    done-callback, killing it before it could emit the artwork phase-done —
    stranding ``"artwork"`` in ``_post_sync_pending`` until the 1800s
    watchdog. That is why cancelling a sync mid-artwork stuck the bar.
    """
    if future.cancelled():
        return None
    try:
        return future.result()
    except asyncio.CancelledError:
        return None


def _log_batch_result(
    future: asyncio.Future[list[Any]], label: str,
) -> None:
    """Log a completion summary for a batch artwork / metadata gather."""
    results = _batch_results(future)
    if results is None:
        logger.info("%s batch was cancelled", label)
        return
    downloaded = sum(1 for r in results if r == "cover-saved")
    existing = sum(1 for r in results if r == "cover-exists")
    no_match = sum(1 for r in results if r == "no-cover-found")
    skipped = sum(1 for r in results if r == "skipped")
    # Cancelled children are counted apart from real errors. ``gather``
    # runs with ``return_exceptions=True``, so cancelling the batch does
    # not cancel the gathering future — each child's CancelledError comes
    # back as a *result*. Lumping those in with errors meant one
    # superseded 1242-game batch logged 1242 "artwork fetch error:
    # CancelledError" warnings describing nothing wrong.
    cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
    errors = [
        r for r in results
        if isinstance(r, BaseException)
        and not isinstance(r, asyncio.CancelledError)
    ]
    logger.info(
        "%s artwork batch finished: %d covers saved, %d already on disk, "
        "%d no match, %d skipped, %d cancelled, %d errors — %d total",
        label, downloaded, existing, no_match, skipped, cancelled,
        len(errors), len(results),
    )
    for r in errors:
        logger.warning(
            "%s artwork fetch error: %s: %s", label, type(r).__name__, r,
        )


def _emit_artwork_phase_done(
    bus: Any, total: int, sync_kwargs: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget ``POST_SYNC_PHASE_CHANGED(phase='artwork', active=False)``.

    Reused from the batch's done-callback (success path) *and*
    from every early-return path in ``_on_sync_complete`` (skip
    paths). Without an emit on skip, ``SyncService._post_sync_pending``
    keeps ``"artwork"`` forever — ``mark_complete()`` never fires
    and the progress bar stalls below 100%.

    Forwards ``sync_kwargs`` so the downstream phase listener
    (CompatibilityService) keeps access to the original
    SYNC_COMPLETE payload — same pattern MetadataService uses to
    hand the kwargs to us.
    """
    if bus is None:
        return
    sk = sync_kwargs or {}
    _track(asyncio.ensure_future(bus.emit(
        Events.POST_SYNC_PHASE_CHANGED,
        phase="artwork", active=False, total=total, done=total,
        sync_kwargs=sk,
        # Echo the generation this batch belongs to. ``SyncService`` and
        # the frontend both drop a phase-done from a superseded run, so
        # a batch that outlives its sync can no longer mark the *current*
        # sync complete. See ``core/sync_generation.py``.
        run_id=run_id_of(sk),
    )))


def _on_artwork_batch_done(
    future: asyncio.Future[list[Any]], bus: Any,
    sync_kwargs: dict[str, Any] | None = None,
) -> None:
    """Done callback: log the batch result + emit POST_SYNC_PHASE_CHANGED.

    The phase-done emit is unconditional — including for a cancelled batch.
    A user-cancelled sync must still drain ``"artwork"`` from
    ``_post_sync_pending`` or the progress bar never completes. (A batch
    cancelled by *replacement* never reaches here: its finisher returns
    early, because the replacing batch owns the phase.)
    """
    _log_batch_result(future, "[ArtworkService]")
    total = len(_batch_results(future) or [])
    _emit_artwork_phase_done(bus, total, sync_kwargs)

# Store id → SteamGridDB title for auth shortcuts. SGDB has art
# for "Amazon Games", not for "amazon" or "Amazon Games Sign-In".
# Reference data — kept here so the auth-shortcut handler stays
# short and the table is greppable from anywhere.
_AUTH_TITLE_FOR_LOOKUP: dict[str, str] = {
    "amazon": "Amazon Games",
    "epic": "Epic Games",
    "gog": "GOG Galaxy",
    "microsoft": "Xbox",
    "ubisoft": "Ubisoft Connect",
    "battlenet": "Battle.net",
}


class _EventHandlersMixin:
    """EventBus subscribers for artwork fetching."""

    # Handlers assume host provides fetch_artwork
    # async def fetch_artwork(self, app_id: int, store: str, game_id: str, title: str) -> dict: ...

    # There is deliberately no install-time handler. A game's shortcut —
    # and therefore its artwork — is created at SYNC time; installing only
    # flips that shortcut's install tag and preserves its appid, so the
    # cover art already exists by then. ``DownloadWorker`` documents the
    # same reasoning where it declines to emit on install completion.
    # A ``GAME_INSTALLED`` handler used to sit here reading ``app_id``; the
    # event had no live emitter and never sent that key.

    @subscribe(Events.ARTWORK_REQUEST)
    async def _on_artwork_request(self: Any, **kwargs: Any) -> None:
        """Handle on-demand artwork fetch requests.

        Contract: ``app_id`` + ``title`` required. ``force=True``
        bypasses the "already has artwork" check (useful on
        account switch when existing art is stale). ``store`` /
        ``game_id`` optional — SteamGridDB only needs the title.
        """
        app_id = kwargs.get("app_id")
        title = kwargs.get("title")
        force = kwargs.get("force", False)
        store = kwargs.get("store", "unknown")
        game_id = kwargs.get("game_id", "unknown")

        if not app_id or not title:
            return

        _track(asyncio.create_task(
            self.fetch_artwork(app_id, store, game_id, title, force=force)
        ))

    @subscribe(Events.SHORTCUT_CREATED)
    async def _on_shortcut_created(self: Any, **kwargs: Any) -> None:
        """Fetch a cover for a newly-created shortcut.

        Only acts on auth shortcuts (``is_auth=True``) — game
        shortcuts get artwork from the bulk post-sync phase
        below, with richer data. Uses ``_AUTH_TITLE_FOR_LOOKUP``
        to map the store id to what SGDB actually has art for.
        """
        is_auth = kwargs.get("is_auth", False)
        if not is_auth:
            return

        app_id = kwargs.get("app_id")
        store = kwargs.get("store")
        title = kwargs.get("title")

        if not app_id or not store:
            return

        sgdb_title = _AUTH_TITLE_FOR_LOOKUP.get(store, title or store)

        _track(asyncio.create_task(
            self.fetch_artwork(app_id, store, "auth", sgdb_title)
        ))

    @subscribe(Events.POST_SYNC_PHASE_CHANGED)
    async def _on_metadata_phase_done(self: Any, **kwargs: Any) -> None:
        """Bulk-fetch artwork once MetadataService has finished its phase.

        Previously subscribed directly to ``SYNC_COMPLETE``, which
        meant Artwork, Metadata, and Compatibility all hit Steam's
        ``storesearch`` endpoint in parallel for every game (3×N
        calls) and triggered the upstream rate-limit. Steam started
        returning empty ``items`` for half of them, so
        ``MetadataService.fetch_appdetails_for_game`` (which needs
        the real Steam AppID) silently produced ``None`` for every
        game and wrote nothing to ``steam_metadata_cache``.

        Switching the trigger to MetadataService's phase-done event
        serialises the three services: Metadata runs alone (no
        contention, populates the ``steam_real_appid`` cache), then
        Artwork starts and reads from that cache for its Phase-3
        Steam-CDN lookup, then Compat starts and reads from the
        same cache for its ProtonDB lookup. Total wall-clock is
        ``T_m + T_a + T_c`` instead of ``max(T_m, T_a, T_c)`` but
        the *result* is actually correct.

        The handler filters for the precise phase + ``active=False``
        flank so it only fires on completion, not on the initial
        start emit.

        Payload contract: ``sync_kwargs`` carries the original
        SYNC_COMPLETE payload (``games``, ``fetch_artwork``,
        ``resync_artwork``) — MetadataService forwards it through
        its phase emit so we keep the same downstream contract.
        Falls back to ``kwargs`` directly for compatibility with
        any code path that hasn't been migrated yet.
        """
        if kwargs.get("phase") != "metadata" or kwargs.get("active") is not False:
            return
        # Same dead fallback as CompatibilityService carried; no emitter has
        # ever sent a flat ``games``. Audit register item 41.
        sync_kwargs = kwargs.get("sync_kwargs") or {}
        games = sync_kwargs.get("games") or []
        bus = getattr(self, "_bus", None)
        grid_dir = getattr(self, "_grid_dir", None)
        if not bool(sync_kwargs.get("fetch_artwork", True)):
            logger.info("[ArtworkService] fetch_artwork=False — skipping phase")
            _emit_artwork_phase_done(bus, 0, sync_kwargs)
            return
        if bool(sync_kwargs.get("skip_chain")):
            # SyncService determined this run covers exactly the store set
            # and game count of the last completed chain, so every game's
            # gaps were already resolved. Announce the phase and hand off.
            logger.info(
                "[ArtworkService] artwork skipped — library unchanged "
                "since the last completed chain",
            )
            self._cancel_prior_batch()
            _emit_artwork_phase_done(bus, 0, sync_kwargs)
            return
        if not games or not grid_dir:
            if not grid_dir:
                logger.warning(
                    "[ArtworkService] _grid_dir unset — cannot save covers",
                )
            _emit_artwork_phase_done(bus, 0, sync_kwargs)
            return
        resync_artwork = bool(sync_kwargs.get("resync_artwork", False))
        if resync_artwork:
            self._clear_resync_cache()
        logger.info(
            "[ArtworkService] phase=metadata done → checking artwork "
            "for %d games (grid_dir=%s, resync=%s)",
            len(games), grid_dir, resync_artwork,
        )
        self._dispatch_artwork_batch(
            games, grid_dir, bus, sync_kwargs, resync=resync_artwork,
        )

    def _clear_resync_cache(self: Any) -> None:
        """Clear the SGDB attempt caches so resync refetches all games.

        Without this, games whose missing-kind set is unchanged are
        skipped; the ``force`` fetch below also bypasses the per-kind
        on-disk check so every game gets a fresh download. Also clears
        the legacy ``sgdb_fetch`` namespace so old installs upgrading
        from the timestamp-cooldown era don't keep stale entries.
        """
        cache = getattr(self, "_cache", None)
        if cache is None:
            return
        for namespace in ("artwork_attempts", "sgdb_fetch"):
            try:
                cache.clear(namespace)
            except Exception:
                logger.exception(
                    "[ArtworkService] failed to clear %s cache", namespace,
                )
        logger.info(
            "[ArtworkService] resync_artwork=True — cleared SGDB attempt caches",
        )

    def _dispatch_artwork_batch(
        self: Any,
        games: list[Any],
        grid_dir: Any,
        bus: Any,
        sync_kwargs: dict[str, Any],
        *,
        resync: bool,
    ) -> None:
        """Fan out per-game artwork fetches and wire the completion callback.

        Single-flight with replace, mirroring
        ``MetadataService._on_sync_complete``: a batch still running when a
        newer sync arrives is cancelled rather than abandoned. Leaving it
        running was the defect behind the whole overlapping-sync cluster —
        measured 2026-08-29, three batches alive at once, the 645-game
        generation announcing its artwork phase done at 02:17:12 against a
        library that had been 1242 games since 02:15:00. That stale
        announcement drained the live run's pending set (restart modal fired
        early), re-triggered CompatibilityService, rewrote shortcut icons
        from a stale generation, and stole two thirds of the shared
        download semaphore from the batch that was actually current.

        It also caused redundant downloads: each game's on-disk gap is
        sampled when its task starts, so a batch that samples while another
        is still writing sees gaps that are about to be filled and fetches
        them again. The 1229-game batch saved 783 covers where only 584 were
        new — 199 wasted round-trips.
        """
        self._cancel_prior_batch()
        if not games:
            _emit_artwork_phase_done(bus, 0, sync_kwargs)
            return
        # ``superseded`` is flipped by ``_cancel_prior_batch`` on the run
        # that replaces this one. A batch cancelled that way must NOT
        # announce the artwork phase — the replacing batch owns it now.
        # A batch cancelled by *user* cancel still announces, or
        # ``_post_sync_pending`` keeps "artwork" forever and the progress
        # bar never completes. Same distinction MetadataService draws with
        # ``cancelled_by_replace``.
        state: dict[str, bool] = {"superseded": False}
        fut = self._spawn_batch(games, grid_dir, bus, resync=resync)
        fut.add_done_callback(
            self._make_batch_finisher(bus, sync_kwargs, state),
        )
        _track(fut)
        # Stash on ``self`` so the SYNC_CANCELLED handler can cancel the
        # whole batch in one shot, and so the next dispatch can replace it.
        self._batch_task = fut
        self._batch_state = state

    def _spawn_batch(
        self: Any, games: list[Any], grid_dir: Any, bus: Any, *, resync: bool,
    ) -> Any:
        """Gather every game's artwork pass under a concurrency gate.

        Bounding matters. Each task begins with ``get_missing_kinds``,
        which stats up to two candidate filenames for each of the five
        kinds, so an unbounded gather over 1242 games queued ~12k stat
        calls at once — on the same loop as the shortcut reconcile, whose
        handler-watchdog timeout then fired *late* (9.5s and 11.6s against
        a 5s budget), the signature of a starved loop. The inner
        per-download semaphore in ``fetch_artwork`` is a separate, smaller
        gate and stays the real network throttle.
        """
        gate = asyncio.Semaphore(self._batch_concurrency())

        async def _one(game: Any) -> Any:
            async with gate:
                return await self._process_one_game(
                    game, grid_dir, bus, force=resync,
                )

        return asyncio.ensure_future(
            asyncio.gather(*(_one(g) for g in games), return_exceptions=True),
        )

    def _make_batch_finisher(
        self: Any, bus: Any, sync_kwargs: dict[str, Any],
        state: dict[str, bool],
    ) -> Any:
        """Build the batch done-callback.

        Closes over ``sync_kwargs`` so CompatibilityService (which waits on
        phase="artwork" done) receives the original SYNC_COMPLETE payload,
        and over ``state`` so a superseded batch stays silent. Flushes the
        deferred attempts-cache writes either way — partial results are
        still valid results.
        """
        def _finish(f: Any) -> None:
            self._flush_artwork_caches()
            if state["superseded"]:
                logger.info(
                    "[ArtworkService] batch superseded by a newer sync — "
                    "not signalling the artwork phase",
                )
                return
            _on_artwork_batch_done(f, bus, sync_kwargs)
        return _finish

    def _batch_concurrency(self: Any) -> int:
        """How many games may be in the artwork pipeline at once.

        A multiple of the per-download semaphore so the cheap on-disk gap
        checks keep the (slower) download gate fed without queueing a stat
        call for every game in the library at t=0.
        """
        base = getattr(self, "_max_concurrent", 0) or 0
        return max(8, int(base) * 3)

    def _cancel_prior_batch(self: Any) -> None:
        """Cancel a still-running batch, marking it superseded first.

        Order matters: the flag has to be set before ``cancel()`` because
        the done-callback can run as soon as the loop next ticks.
        """
        prior = getattr(self, "_batch_task", None)
        if prior is None or prior.done():
            return
        prior_state = getattr(self, "_batch_state", None)
        if isinstance(prior_state, dict):
            prior_state["superseded"] = True
        logger.info(
            "[ArtworkService] cancelling in-flight artwork batch — "
            "a newer sync replaced it",
        )
        prior.cancel()

    @subscribe(Events.SYNC_CANCELLED)
    async def _on_sync_cancelled(self: Any, **_kwargs: Any) -> None:
        """Cancel the in-flight artwork batch on user cancel."""
        task = getattr(self, "_batch_task", None)
        if task is not None and not task.done():
            task.cancel()

    @subscribe(Events.SHORTCUT_REMOVED)
    async def _on_shortcut_removed(self: Any, **kwargs: Any) -> None:
        """Delete a game's grid artwork when its shortcut is removed.

        Without this, every game dropped during a normal sync (no
        longer owned, store signed out, library churn) leaks its
        artwork forever — the files outlive the shortcut and, because
        shortcut appids are deterministic, get mistaken for valid
        "already on disk" art when the same game is later re-synced.
        Cleaning art at removal keeps the grid dir in lockstep with the
        live shortcut set.
        """
        from .fetcher import delete_artwork_files

        # Bulk "delete all data" sets this flag and does one broad grid
        # sweep itself — skip the per-game delete so we don't rescan the
        # grid dir once per removed shortcut.
        if getattr(self, "_suppress_removal_cleanup", False):
            return
        app_id = kwargs.get("app_id")
        if not isinstance(app_id, int):
            return
        grid_dir = getattr(self, "_grid_dir", None)
        if not grid_dir:
            return
        deleted = await delete_artwork_files(grid_dir, app_id)
        if deleted:
            logger.info(
                "[ArtworkService] removed %d artwork file(s) for appid %d",
                deleted, app_id,
            )

    async def _process_one_game(
        self: Any, game: Game, grid_dir: str, bus: Any,
        *, force: bool = False,
    ) -> str:
        """Resolve artwork for a single game; return a status tag.

        Returns ``"cover-saved"``, ``"cover-exists"``,
        ``"no-cover-found"``, ``"cancelled"``, or ``"skipped"``.
        Calls ``increment_artwork`` on the shared ``SyncProgress``
        instance (via ``bus.get_sync_progress()``) so the frontend
        progress bar ticks up per game — mirroring staging's
        ``sync_progress.increment_artwork()`` pattern.

        Args:
            force: bypass the per-kind on-disk skip check. Set by the
                SYNC_COMPLETE handler when ``resync_artwork=True`` so
                the Force-Sync modal's "re-download artwork" choice
                actually re-downloads every kind.
        """
        from .fetcher import get_missing_kinds

        # Cancel checkpoint — SyncService.cancel() flips progress.status
        # to "cancelled". Queued tasks short-circuit here instead of doing
        # network work the user no longer cares about. The phase-done event
        # in the batch's finally still fires, so _post_sync_pending clears.
        progress = _sync_progress(bus)
        if progress is not None and progress.status == "cancelled":
            return "cancelled"

        if not game.app_id or not game.title:
            return "skipped"
        # Per-kind gap detection (was a coarse grid+hero gate that
        # stranded logo/icon/landscape forever). Pass the exact missing
        # kinds so fetch_artwork backfills only those.
        missing = None if force else await get_missing_kinds(grid_dir, game.app_id)
        if missing is not None and not missing:
            return "cover-exists"
        extras = getattr(game, "metadata", None)
        result = await self.fetch_artwork(
            game.app_id, game.store, game.store_game_id, game.title,
            extras=extras, force=force, only_kinds=missing,
        )
        # Tick the progress bar — SyncService puts the tracker on the bus
        # in _setup_sync and clears it on completion.
        progress = _sync_progress(bus)
        if progress is not None:
            await progress.increment_artwork(game.title)
        # "saved" only when a kind we were actually after got filled —
        # pre-existing covers (True in result but not in `missing`) don't
        # count, so the batch summary stays meaningful for backfills.
        target_kinds = missing if missing is not None else set(result.keys())
        filled = any(result.get(k) for k in target_kinds)
        return "cover-saved" if filled else "no-cover-found"
