"""services/size_backfill.py — post-sync warm-up of the download-size cache.

"Space Required" is fetched lazily, per game, the first time its App-Details
page opens. That lookup is a live store call — ``legendary info`` for Epic,
the gogdl install planner for GOG, ``nile install --info`` for Amazon — each a
network round-trip taking seconds. The result is already persisted
(:class:`~unifideck.services.size_cache.SizeCache`), so the cost is paid once
per game, but it is paid *in front of the user*: on a real device only 51 of
611 owned Epic/GOG/Amazon games had a cached size, so ~92% of page opens sat
on a visible multi-second gap before the number appeared.

This walks the library in the background after a sync and fills the same
cache, so by the time a page is opened the value is already there.

Deliberately NOT a registered post-sync phase. ``SyncService`` gates
``mark_complete`` on every registered phase reporting done; a long network
walk that can be cancelled or stall would risk stranding the progress bar at
<100% (the failure mode called out in ``MetadataService._on_sync_complete``).
Instead this is fire-and-forget, exactly like ``metadata_backfill``: the sync
UI completes immediately and sizes land quietly as they resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from unifideck.services.size_cache import get_size_cache

if TYPE_CHECKING:
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)

# Strong refs to fire-and-forget tasks — asyncio only holds a weak one, so
# without this the walk could be collected mid-flight (RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Concurrency is PER STORE, and deliberately 1.
#
# Running two lookups against the same store in parallel makes them fail:
# 63 GOG games came back empty during a concurrent walk, yet every one of
# them resolved first try when called on its own (5.18 GB, 41.56 GB, …).
# gogdl invocations share a persistent GOGDL_CONFIG_PATH holding its
# manifest and dependencies-repo caches, so two processes race on it — the
# same shared-state hazard that previously hung installs at "Getting
# Dependencies repository".
#
# Different stores are independent processes with independent caches, so
# they still run in parallel: the walk is as wide as the number of stores.
PER_STORE_CONCURRENCY = 1

# Per-game ceiling. A store that hangs must not park a worker slot forever.
LOOKUP_TIMEOUT_S = 30

# Stores whose ``get_game_size`` returns a real answer. Every store
# *overrides* the method — it is abstract on ``StoreBase`` — so this cannot be
# derived with ``hasattr``: Ubisoft's body is a literal ``return None`` (no
# download-size API), and walking it would be pure waste.
#
# It has to be a written list, so it has to be MAINTAINED. GameVault was
# absent from it for its first release and every one of its games showed
# "Size: 0 MB" in App Details while the download row, which reads the same
# HEAD at install time, showed the real 34.6 GB. Battle.net is deliberately
# absent: its size comes from ``product.db`` and is 0 until the client
# finishes writing, so a pre-install lookup has nothing to return.
SIZE_CAPABLE_STORES = frozenset({"epic", "gog", "amazon", "gamevault"})


def is_running() -> bool:
    """True while a warm-up walk is in flight."""
    return any(not t.done() for t in _BACKGROUND_TASKS)


def cancel() -> None:
    """Stop any in-flight walk (a sync is starting).

    A walk resumed at boot can still be running when the user kicks off a
    sync, and it would then compete for bandwidth and store rate limits with
    the metadata / artwork / compat phases — the exact contention the
    post-sync spawn point exists to avoid.

    Cancelling costs nothing: sizes are written through as they resolve, so
    the finished ones stay on disk and the walk re-spawns when the sync's
    phases complete, picking up only what is still missing.
    """
    for task in list(_BACKGROUND_TASKS):
        if not task.done():
            task.cancel()


def spawn(registry: Any, games: list[Game], cache_path: str) -> None:
    """Schedule a fire-and-forget size warm-up for ``games``.

    No-ops on empty input, a missing registry, or when a walk is already
    running — there are two triggers (plugin boot and post-sync completion)
    and they can coincide: boot starts a resume, the user immediately syncs,
    and the second spawn would double the store calls while both passes race
    on the same not-yet-cached games. Whichever walk is already going will
    cover the library anyway.

    Never raises into the caller — a warm-up failure must not affect the sync
    it follows.
    """
    if not games or registry is None:
        return
    if is_running():
        logger.debug("[SizeBackfill] walk already in flight — not spawning")
        return
    task = asyncio.create_task(
        _run(registry, games, cache_path),
        name="size-backfill",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _pending(games: list[Game]) -> list[Game]:
    """Games worth a lookup: size-capable store, not installed, has an id.

    Installed games are skipped because their "Installed Size" comes from a
    local directory walk, not the store — a different (and much cheaper)
    path that needs no warming.
    """
    out: list[Game] = []
    for g in games:
        store = getattr(g, "store", "")
        game_id = getattr(g, "store_game_id", "")
        if store not in SIZE_CAPABLE_STORES or not game_id:
            continue
        if getattr(g, "installed", False):
            continue
        out.append(g)
    return out


async def _run(registry: Any, games: list[Game], cache_path: str) -> None:
    """Fill the size cache for every not-yet-cached game in ``games``."""
    cache = get_size_cache(cache_path)
    candidates = _pending(games)
    # Filter against the cache first so a warm library costs zero lookups
    # and the log line reports honest numbers.
    pending: list[Game] = []
    for game in candidates:
        store = game.store
        game_id = game.store_game_id
        skip = False
        with contextlib.suppress(Exception):
            # Skip both a known size AND a recent failure — re-walking games
            # the store cannot answer for just burns the whole window on
            # them (measured: 142 of 277 in one pass) and starves the ones
            # that would have resolved.
            skip = (
                await cache.get(store, game_id) is not None
                or await cache.is_unknown(store, game_id)
            )
        if skip:
            continue
        pending.append(game)
    if not pending:
        logger.info("[SizeBackfill] all %d sizes cached", len(candidates))
        return
    logger.info(
        "[SizeBackfill] warming %d of %d download sizes",
        len(pending), len(candidates),
    )
    # One semaphore per store, so gogdl never races another gogdl while
    # Epic and Amazon still proceed alongside it.
    sems: dict[str, asyncio.Semaphore] = {
        s: asyncio.Semaphore(PER_STORE_CONCURRENCY) for s in SIZE_CAPABLE_STORES
    }
    results = await asyncio.gather(
        *(_fill_one(registry, cache, sems[g.store], g) for g in pending),
        return_exceptions=True,
    )
    filled = sum(1 for r in results if r is True)
    logger.info(
        "[SizeBackfill] complete — %d/%d sizes resolved",
        filled, len(pending),
    )


async def _fill_one(
    registry: Any, cache: Any, sem: asyncio.Semaphore, game: Game,
) -> bool:
    """Resolve and persist one game's download size. Never raises.

    A failure here is deliberately NOT recorded as "unknown". The walk runs
    unattended and its misses are not always the store's fault — a whole
    batch of GOG games came back empty purely from parallel invocations and
    resolved fine individually. Writing a stamp on that would suppress the
    on-demand lookup too, hiding sizes that actually work. Only the
    user-facing RPC, whose single attempt is the thing whose latency we are
    protecting, records a miss.
    """
    async with sem:
        store = game.store
        game_id = game.store_game_id
        try:
            adapter = registry.get_store(store)
        except Exception:
            return False
        if adapter is None or not hasattr(adapter, "get_game_size"):
            return False
        try:
            size = await asyncio.wait_for(
                adapter.get_game_size(game_id), timeout=LOOKUP_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "[SizeBackfill] %s:%s lookup failed", store, game_id,
                exc_info=True,
            )
            return False
        size_int = int(size or 0)
        if size_int <= 0:
            return False
        with contextlib.suppress(Exception):
            await cache.put(store, game_id, size_int)
        return True
