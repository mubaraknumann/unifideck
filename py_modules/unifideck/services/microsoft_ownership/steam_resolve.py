"""Which Steam game each owned Xbox product is.

Two sources, cheapest first:

- **The library's own mapping.** An owned product that also streams has an
  xCloud library row, and the metadata phase already mapped that row to a
  Steam AppID (``steam_real_appid``, keyed by the row's shortcut AppID).
- **Steam's store search**, for products with no library row (owned but not
  streamable). One request per product, one at a time, after the sync's own
  storesearch users (metadata, artwork, compat) have finished; capped per
  run so a first run over a large library finishes over a few syncs rather
  than flooding Steam.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from unifideck.core.steam_appid_map import read_positive_steam_appid
from unifideck.core.types.domain import Game
from unifideck.steam.library import search_store

from .index import STEAM_MISS, STEAM_SEARCH_REVISION, needs_steam_search

logger = logging.getLogger(__name__)

#: Steam searches per refresh. Matches the size of a large Xbox library, so
#: one sync usually finishes; a bigger one completes over the next syncs.
SEARCH_BUDGET = 150
_SAVE_EVERY = 25
_SEARCH_PAUSE_SECONDS = 0.25


def seed_from_library(index: dict[str, Any], games: Iterable[Game], cache: Any) -> int:
    """Copy the library's Steam mapping onto owned products; the number copied."""
    products = index.get("products", {})
    copied = 0
    for game in games:
        if game.store != "microsoft":
            continue
        entry = products.get((game.store_game_id or "").upper())
        if not isinstance(entry, dict) or (entry.get("steam_appid") or 0) > 0:
            continue
        appid = read_positive_steam_appid(cache, game.app_id)
        if appid:
            entry["steam_appid"] = appid
            entry["steam_checked_at"] = time.time()
            copied += 1
    return copied


async def resolve_pending(
    index: dict[str, Any], config: Any, save: Callable[[], None],
    budget: int = SEARCH_BUDGET,
) -> tuple[int, int]:
    """Search Steam for products not mapped yet: ``(matched, missed)``."""
    todo = [e for e in index.get("products", {}).values()
            if isinstance(e, dict) and needs_steam_search(e)][:budget]
    matched = missed = 0
    for done, entry in enumerate(todo, 1):
        appid = await _steam_appid(entry["title"], config)
        entry["steam_appid"] = appid if appid > 0 else STEAM_MISS
        entry["steam_checked_at"] = time.time()
        entry["steam_search_rev"] = STEAM_SEARCH_REVISION
        matched += appid > 0
        missed += appid <= 0
        if done % _SAVE_EVERY == 0:
            save()
        await asyncio.sleep(_SEARCH_PAUSE_SECONDS)
    return matched, missed


async def _steam_appid(title: str, config: Any) -> int:
    """The Steam AppID for ``title``, or 0. Never raises."""
    try:
        result = await search_store(title, config=config)
        return int(result["app_id"]) if result else 0
    except (TypeError, ValueError, KeyError):
        return 0
    except Exception as e:
        logger.debug("[MicrosoftOwnership] Steam search failed for %r: %s", title, e)
        return 0
