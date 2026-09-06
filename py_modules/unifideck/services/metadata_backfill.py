"""services/metadata_backfill.py — post-sync background backfill.

Lives next to :mod:`metadata_service` so that file stays under the
``550``-LOC volumetry cap. The split is along an axis the caller
already knows about:

* :class:`MetadataService` owns the *blocking* enrichment phase —
  every game gets its Steam Store search, appdetails, and UnifiDB
  lookup before ``POST_SYNC_PHASE_CHANGED(phase="metadata")`` fires.
* This module owns the *fire-and-forget* backfill that runs **after**
  the phase emit. The sync UI reaches 100% the moment Metadata's
  blocking phase finishes; the long-tail Metacritic lookups happen
  in the background and quietly land in the cache as they complete.

Why split Metacritic out of the per-game gather:

Steam's ``appdetails`` payload embeds ``metacritic.score`` for the
vast majority of titles that have one. Calling
``backend.metacritic.com`` for *every* game during sync was paying
~300-1500ms per game for data we usually already have. The cost
falls to ~5% of the library (the long tail of indie / niche titles
not covered by Steam's embedded score) and we pay it without
blocking the sync's progress bar.

Two small cache-reader helpers come along for the ride because
they're only used by the backfill (``MetadataService`` itself reads
the same caches through different paths).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.steam_appid_map import read_positive_steam_appid
from unifideck.core.types.events import Events
from unifideck.services import metadata_sources
from unifideck.services.metadata_steam_mixin import CACHE_NAMESPACE

if TYPE_CHECKING:
    from unifideck.core.cache_manager import CacheManager
    from unifideck.core.types import Game
    from unifideck.services.metadata_service import MetadataService

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget backfill tasks. ``asyncio``
# keeps only a weak reference to a task, so without this set a
# running backfill could be garbage-collected mid-flight (RUF006).
# The done-callback discards the entry once the task settles.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Mirrors ``metadata_service``'s constants — duplicated here rather
# than imported so a future refactor of the service can rename the
# namespaces without breaking the backfill. The wire-level cache
# layout is the boundary; the constant names are not.
_STEAM_METADATA_NS = "steam_metadata"

# Backfill is background work — keep concurrency low so we don't
# pile load onto Metacritic's CDN even though we're not in the
# critical path. 3 parallel sessions is plenty for a 1000-game
# library (long-tail is ~50 games), finishes in 1-2 minutes.
BACKFILL_CONCURRENCY = 3


def spawn(service: MetadataService, games: list[Game]) -> None:
    """Schedule a fire-and-forget Metacritic backfill for ``games``.

    Called from :meth:`MetadataService._run_enrichment`'s ``finally``
    block after the phase-done emit. The created task runs
    independently of the sync's lifecycle; it doesn't gate progress
    completion or block sync re-runs.

    No-ops on empty input.
    """
    if not games:
        return
    task = asyncio.create_task(
        _run(service, games),
        name="metacritic-backfill",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _run(service: MetadataService, games: list[Game]) -> None:
    """Walk ``games``, calling :meth:`MetadataService._fetch_metacritic`
    only for the ones whose cached Steam appdetails didn't already
    supply a critic score.

    Results land in the composite ``metadata`` cache so the panel's
    ``enrich()`` fallback picks them up on next read. Exceptions are
    swallowed per-game; backfill failure must not gate anything.
    """
    sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)
    await asyncio.gather(
        *(_fill_one(service, sem, g) for g in games),
        return_exceptions=True,
    )
    with contextlib.suppress(Exception):
        service._cache.flush(CACHE_NAMESPACE)
    logger.info(
        "[MetadataBackfill] metacritic backfill complete (%d games)",
        len(games),
    )
    # Tell the frontend the long-tail scores have landed so it re-reads
    # library facets — otherwise newly-backfilled metacritic only shows
    # after a manual resync/restart (the facet read on sync-complete
    # races this background task).
    bus = getattr(service, "_bus", None)
    if bus is not None:
        with contextlib.suppress(Exception):
            await bus.emit(
                Events.METADATA_BACKFILL_COMPLETE,
                count=len(games),
            )


async def _fill_one(
    service: MetadataService,
    sem: asyncio.Semaphore,
    game: Game,
) -> None:
    """Backfill Metacritic for a single game, gated on the semaphore.

    Skips the live fetch entirely when a score is already known —
    either from Steam's embedded ``metacritic.score`` (the common
    case) **or** from a previous backfill pass that wrote into the
    composite ``metadata`` cache. The second check is what makes
    this a backfill rather than a re-download: once we've populated
    a game's score, subsequent syncs don't hit the Metacritic
    backend again.
    """
    cache = service._cache
    if _already_has_metacritic(cache, game):
        return
    async with sem:
        with contextlib.suppress(Exception):
            data = await metadata_sources.fetch_metacritic(game.title, config=service._config)
            if data:
                _merge_into_metadata_cache(cache, game, data)


def _already_has_metacritic(cache: CacheManager, game: Game) -> bool:
    """True iff we already have a Metacritic score for ``game`` from any source.

    Two sources count as "already have it":

    1. Steam's appdetails payload embeds a critic score for the
       game (the path that covers most titles after the main
       enrichment phase has run).
    2. The composite ``metadata`` namespace contains a
       ``metacritic_score`` integer — written by a prior backfill
       pass, ``MetadataService._fetch_metacritic`` direct calls,
       or any future code path that knows what it's doing.

    Returning True from either branch keeps backfill idempotent
    across syncs: once a game is filled, it stays filled.
    """
    if _has_steam_metacritic(cache, game):
        return True
    return _has_cached_metacritic(cache, game)


def _has_steam_metacritic(cache: CacheManager, game: Game) -> bool:
    """``metacritic.score`` came in via the Steam appdetails fetch."""
    steam_id = read_positive_steam_appid(cache, game.app_id)
    if not steam_id:
        return False
    steam_meta = _read_steam_metadata(cache, steam_id)
    mc = steam_meta.get("metacritic") if isinstance(steam_meta, dict) else None
    if not isinstance(mc, dict):
        return False
    return isinstance(mc.get("score"), int)


def _has_cached_metacritic(cache: CacheManager, game: Game) -> bool:
    """A prior backfill (or any other writer) already populated the score."""
    cache_key = f"{game.store}:{game.store_game_id}"
    try:
        entry = cache.get(CACHE_NAMESPACE, cache_key)
    except Exception:
        return False
    if not isinstance(entry, dict):
        return False
    return isinstance(entry.get("metacritic_score"), int)


def _merge_into_metadata_cache(
    cache: CacheManager,
    game: Game,
    data: dict[str, Any],
) -> None:
    """Merge fresh ``_fetch_metacritic`` output into the composite cache.

    Drops the ``_negative`` sentinel if previously set — the
    backfill produced real data so the next ``enrich()`` shouldn't
    short-circuit.
    """
    cache_key = f"{game.store}:{game.store_game_id}"
    existing = cache.get(CACHE_NAMESPACE, cache_key)
    merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    merged.update(data)
    merged.pop("_negative", None)
    # Stamp the resolved real Steam AppID when known. The facet now reads
    # metacritic by the ``store:game_id`` key, but keeping steam_appid on
    # the entry keeps any steam_appid-keyed reader correct (defensive).
    if not merged.get("steam_appid"):
        steam_id = read_positive_steam_appid(cache, game.app_id)
        if steam_id:
            merged["steam_appid"] = steam_id
    # Deferred write — one per backfilled game; ``_run`` flushes once
    # after the gather.
    cache.set(CACHE_NAMESPACE, cache_key, merged, flush=False)




def _read_steam_metadata(
    cache: CacheManager,
    steam_id: int,
) -> dict[str, Any]:
    """Return cached Steam appdetails for ``steam_id``, or ``{}``."""
    if not steam_id:
        return {}
    try:
        value = cache.get(_STEAM_METADATA_NS, str(steam_id))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}
