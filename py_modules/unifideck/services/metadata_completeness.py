"""Is a game's metadata already fully cached?

Extracted from ``metadata_service.py``, which sat one line under the 550-LOC
volumetry cap. It is a self-contained question — three cache lookups and no
service state — so it reads better as a function than as a method that only
ever touched ``self._cache``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from unifideck.services.metadata_steam_mixin import (
    CACHE_NAMESPACE,
    STEAM_METADATA_NS,
    STEAM_REAL_APPID_NS,
    steam_appid_miss_stale,
)

if TYPE_CHECKING:
    from unifideck.core.cache_manager import CacheManager
    from unifideck.core.types import Game


def has_complete_metadata(cache: CacheManager, game: Game) -> bool:
    """Whether every stage of enrichment already has a cached answer.

    Three stages, each able to short-circuit: the merged metadata payload,
    the game's real Steam AppID, and that AppID's ``appdetails``. A missing
    entry at any stage means the game still needs work.

    The subtle one is a *negative* AppID. It records "no Steam counterpart
    for the title we searched", and the title can change under a stable game
    id — so it only counts as complete while the title still matches. Without
    that check a game renamed after a failed search was complete forever, and
    everything hanging off a real Steam AppID (the ProtonDB / Deck-Verified
    tier most visibly) stayed empty. See ``steam_appid_miss_stale``.
    """
    cache_key = f"{game.store}:{game.store_game_id}"
    # 1. The merged metadata payload. A negative marker counts as an answer
    #    only for the title it was recorded against.
    try:
        cached = cache.get(CACHE_NAMESPACE, cache_key)
        if cached is None or metadata_negative_stale(cached, game.title):
            return False
    except Exception:
        return False

    # A Steam-native game needs nothing below.
    if game.store == "steam":
        return True

    try:
        # 2. The real Steam AppID for this shortcut.
        steam_id = cache.get(STEAM_REAL_APPID_NS, str(game.app_id))
        if steam_id is None:
            return False
        if steam_id <= 0:
            return not steam_appid_miss_stale(cache, game.app_id, game.title)
        # 3. That AppID's rich appdetails.
        if cache.get(STEAM_METADATA_NS, str(steam_id)) is None:
            return False
    except Exception:
        return False

    return True


def negative_marker(title: str) -> dict[str, object]:
    """The cache entry for "no source knew this game", for *title*."""
    return {"_negative": True, "title": title}


def metadata_negative_stale(cached: object, title: str) -> bool:
    """Whether a cached "no metadata" verdict was about a different title.

    The same trap as :func:`steam_appid_miss_stale`, one stage up. Both
    sources search by title, so a miss is a statement about the title that
    was searched. It was cached under the stable game id with no record of
    that title, so a game whose title was later corrected stayed blank for
    good: ``Alan Wake 2 AnkerGames`` matched nothing, and once the uploader
    tag was stripped the clean ``Alan Wake 2`` (which unifiDB knows) was
    never asked about.

    A marker written before the title was recorded has none. Treat it as
    stale: one retry per affected game, then it is recorded properly.
    """
    if not isinstance(cached, dict) or not cached.get("_negative"):
        return False
    return cached.get("title") != title

