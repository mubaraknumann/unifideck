"""services/pcgw_backfill.py — post-sync save-location backfill.

unifiDB ships pre-baked save locations for most games. For the ones it misses
(new releases, niche titles), this fire-and-forget pass fetches them LIVE from
PCGamingWiki — the hybrid fallback — and caches them under ``pcgw_saves`` so the
next launch's save-location resolver finds them without a network round-trip on
the hot path.

Mirrors :mod:`metadata_backfill`:

* runs AFTER the blocking metadata phase, so ``steam_real_appid`` is already
  populated and PCGamingWiki can be joined by the most reliable key;
* low concurrency (PCGamingWiki is a small community wiki — be polite);
* idempotent — skips games already covered by unifiDB *or* a prior PCGW attempt
  (positive or negative), so repeated syncs don't re-hit the wiki.

Only GOG/Epic games are processed — those are the stores whose strategies consult
the save-location resolver.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.steam_appid_map import read_positive_steam_appid

if TYPE_CHECKING:
    from unifideck.core.types import Game
    from unifideck.services.metadata_service import MetadataService

logger = logging.getLogger(__name__)

# Strong refs so fire-and-forget tasks aren't GC'd mid-flight (RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

_METADATA_NS = "metadata"
_PCGW_NS = "pcgw_saves"
_SUPPORTED_STORES = ("gog", "epic")
BACKFILL_CONCURRENCY = 3


def spawn(service: MetadataService, games: list[Game]) -> None:
    """Schedule a fire-and-forget PCGamingWiki save-location backfill.

    Called from :meth:`MetadataService._run_enrichment`'s ``finally`` block,
    after the phase-done emit. No-ops when no GOG/Epic games are present.
    """
    targets = [g for g in games if getattr(g, "store", None) in _SUPPORTED_STORES]
    if not targets:
        return
    task = asyncio.create_task(_run(service, targets), name="pcgw-saves-backfill")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _run(service: MetadataService, games: list[Game]) -> None:
    sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)
    await asyncio.gather(
        *(_fill_one(service, sem, g) for g in games),
        return_exceptions=True,
    )
    # Persist the loop's deferred writes in one go.
    with contextlib.suppress(Exception):
        service._cache.flush(_PCGW_NS)
    logger.info("[PCGWBackfill] save-location backfill complete (%d games)", len(games))


async def _fill_one(
    service: MetadataService, sem: asyncio.Semaphore, game: Game,
) -> None:
    """Fetch + cache PCGamingWiki save locations for one game, if not covered."""
    cache = service._cache
    key = f"{game.store}:{game.store_game_id}"
    if _already_covered(cache, key):
        return
    async with sem:
        with contextlib.suppress(Exception):
            from unifideck.metadata import pcgamingwiki
            steam_appid = read_positive_steam_appid(cache, getattr(game, "app_id", None)) or None
            result = await pcgamingwiki.lookup(
                game.store, game.store_game_id, game.title,
                steam_appid=steam_appid,
                config=getattr(service, "_config", None),
            )
            if result and result.get("save_locations"):
                cache.set(_PCGW_NS, key, result, flush=False)
            else:
                # Negative sentinel: don't re-query a game PCGW doesn't cover.
                cache.set(_PCGW_NS, key, {"_negative": True}, flush=False)


def _already_covered(cache: Any, key: str) -> bool:
    """True if unifiDB already has save locations OR PCGW was already tried."""
    meta = _safe_get(cache, _METADATA_NS, key)
    if isinstance(meta, dict) and meta.get("save_locations"):
        return True
    return isinstance(_safe_get(cache, _PCGW_NS, key), dict)


def _safe_get(cache: Any, namespace: str, key: str) -> Any:
    try:
        return cache.get(namespace, key)
    except Exception:
        return None


