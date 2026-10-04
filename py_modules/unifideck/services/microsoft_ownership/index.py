"""The persisted Xbox purchase index (cache namespace ``microsoft_owned``).

One cache key holds the whole index, so every commit replaces it in one
atomic write and a reader never sees half a list::

    {"version": 1, "fetched_at": <epoch s>, "products": {
        "<PRODUCT ID>": {"title", "pc", "console", "play_anywhere", "gold",
                         "source", "steam_appid", "steam_checked_at",
                         "steam_search_rev"}}}

``steam_appid``: > 0 mapped, :data:`STEAM_MISS` searched without a match,
:data:`STEAM_PENDING` not searched yet. Steam's search answers a network
error and "no such game" the same way, so a miss is retried after
:data:`MISS_RETRY_SECONDS`, as soon as the product's title changes, or at
the next refresh when it was recorded by an older search
(:data:`STEAM_SEARCH_REVISION`).

The registry TTL for this namespace must stay 0: a positive TTL evicts an
entry on read, which would erase the last good list exactly when Microsoft
is unreachable. Freshness lives in ``fetched_at`` instead.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from unifideck.core.cross_store_ownership import PurchasedProduct, PurchaseIndex
from unifideck.stores.microsoft.ownership import OwnedFetch

logger = logging.getLogger(__name__)

CACHE_NAME = "microsoft_owned"
_KEY = "index"
_VERSION = 1
STEAM_PENDING = 0
STEAM_MISS = -1
MISS_RETRY_SECONDS = 7 * 24 * 3600
#: Bump when the Steam title search improves, so misses recorded by the
#: older search are retried at once. 2: platform words ("(PC)", "for XBOX
#: One") are stripped before searching; DOOM Eternal and Mafia: Definitive
#: Edition were misses under 1.
STEAM_SEARCH_REVISION = 2


def load_index(cache: Any) -> dict[str, Any] | None:
    """The stored index, or None when there is none or it is unreadable."""
    try:
        raw = cache.get(CACHE_NAME, _KEY)
    except Exception as e:
        logger.warning("[MicrosoftOwnership] could not read the purchase index: %s", e)
        return None
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("version") != _VERSION
        or not isinstance(raw.get("products"), dict)
    ):
        logger.warning("[MicrosoftOwnership] ignoring a purchase index in an unknown shape")
        return None
    return raw


def save_index(cache: Any, index: dict[str, Any]) -> bool:
    """Persist ``index``; False (logged) if the cache refused."""
    try:
        cache.set(CACHE_NAME, _KEY, index)
        return True
    except Exception as e:
        logger.warning("[MicrosoftOwnership] could not save the purchase index: %s", e)
        return False


def clear_index(cache: Any) -> None:
    """Forget the index (the user signed out of Microsoft)."""
    try:
        cache.delete(CACHE_NAME, _KEY)
    except Exception as e:
        logger.warning("[MicrosoftOwnership] could not clear the purchase index: %s", e)


def merge(previous: dict[str, Any] | None, fetch: OwnedFetch, now: float) -> dict[str, Any]:
    """The new index from ``fetch``, keeping Steam matches from ``previous``.

    A positive match survives a title change (Microsoft retitles editions);
    a miss does not, because the new title deserves a fresh search.
    """
    old = (previous or {}).get("products") or {}
    products: dict[str, dict[str, Any]] = {}
    for pid, p in fetch.products.items():
        found = old.get(pid)
        prior: dict[str, Any] = found if isinstance(found, dict) else {}
        appid = prior.get("steam_appid", STEAM_PENDING)
        if not isinstance(appid, int) or (appid == STEAM_MISS and prior.get("title") != p.title):
            appid = STEAM_PENDING
        searched = appid != STEAM_PENDING
        products[pid] = {
            "title": p.title, "pc": p.pc, "console": p.xbox, "play_anywhere": p.xpa,
            "gold": p.gold, "source": p.source, "steam_appid": appid,
            "steam_checked_at": prior.get("steam_checked_at", 0) if searched else 0,
            "steam_search_rev": prior.get("steam_search_rev", 1) if searched else 0,
        }
    return {"version": _VERSION, "fetched_at": now, "products": products}


def needs_steam_search(entry: dict[str, Any], now: float | None = None) -> bool:
    """Unsearched, or a miss old enough or found by an older search; never
    without a title. A miss without a revision predates revisions: 1."""
    if not entry.get("title"):
        return False
    appid = entry.get("steam_appid", STEAM_PENDING)
    if appid == STEAM_PENDING:
        return True
    if appid != STEAM_MISS:
        return False
    if (entry.get("steam_search_rev") or 1) < STEAM_SEARCH_REVISION:
        return True
    checked = entry.get("steam_checked_at") or 0
    return (now or time.time()) - checked > MISS_RETRY_SECONDS


def to_view(index: dict[str, Any]) -> PurchaseIndex:
    """The join's view: products by id, and Steam AppID → product ids."""
    products: dict[str, PurchasedProduct] = {}
    by_appid: dict[int, list[str]] = {}
    for pid, entry in index.get("products", {}).items():
        if not isinstance(entry, dict):
            continue
        products[pid] = PurchasedProduct(
            title=str(entry.get("title") or ""),
            pc=bool(entry.get("pc")),
            console=bool(entry.get("console")),
            play_anywhere=bool(entry.get("play_anywhere")),
            gold=bool(entry.get("gold")),
        )
        appid = entry.get("steam_appid")
        if isinstance(appid, int) and appid > 0:
            by_appid.setdefault(appid, []).append(pid)
    return PurchaseIndex(
        products=products,
        by_steam_appid={appid: tuple(pids) for appid, pids in by_appid.items()},
    )
