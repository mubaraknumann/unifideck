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
    # 1. The merged metadata payload (a negative marker counts as an answer).
    try:
        if cache.get(CACHE_NAMESPACE, cache_key) is None:
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
