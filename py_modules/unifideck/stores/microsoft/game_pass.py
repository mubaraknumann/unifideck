"""Which xCloud titles are in the Game Pass catalog.

``/v2/titles`` marks a title entitled for two different reasons: it is in the
Game Pass catalog, or the user owns it and it streams as their own game. The
Steam Store ribbon must tell them apart, because they are different facts: an
owned game that streams is "Cloud", a catalog title is "Game Pass". Nothing in
the ``/v2/titles`` row says which.

Microsoft's public Game Pass lists (``catalog.gamepass.com/sigls/v2``) name
the catalog. They often list a bundle or edition rather than the base product
xCloud streams: Forza Horizon 5 streams as ``9NNX1VVR3KNQ`` while Game Pass
lists ``9NKX70BBCDRN``, a bundle that contains it. So a title is in Game Pass
when its own id, or one of its displaycatalog ``RelatedProducts`` of type
``Bundle`` or ``SellableBy``, is on a list.

Measured on 2026-10-04 against a real Ultimate account (market IN): 568 of 576
entitled titles matched, 64 of them only through a related product. The 8 left
were 5 owned purchases that stream as owned (Mafia: Definitive Edition,
Cyberpunk 2077, The Witcher 3, Thief, When the Past was Around) and 3
entitlements the purchase list does not show.

A list that cannot be read makes the whole answer unknown (``None``), never
"not in Game Pass": a partial catalog would call Game Pass titles owned-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any

from unifideck.core.net import ssl_ctx_permissive as _ssl

logger = logging.getLogger(__name__)

_SIGLS_URL = "https://catalog.gamepass.com/sigls/v2"

#: The public Game Pass lists: all console games, all PC games, all games, and
#: EA Play on console and on PC (EA Play is part of Ultimate and PC Game Pass).
GAME_PASS_LIST_IDS: tuple[str, ...] = (
    "f6f1f99f-9b49-4ccd-b3bf-4d9767a77f5e",
    "fdd9e2a7-0fee-49f6-ad69-4354098401ff",
    "29a81209-df6f-41fd-a528-2ae6b91f719c",
    "b8900d09-a491-44cc-916e-32b5acae621b",
    "1d33fbb9-b895-4732-a8ca-a55c8b99fa2c",
)

#: Relationships that mean "this product is sold as part of that one".
_RELATED_TYPES = frozenset({"Bundle", "SellableBy"})


async def fetch_game_pass_ids(market: str) -> frozenset[str] | None:
    """Every product id on the Game Pass lists for *market*, UPPER-case.

    ``None`` when any list could not be read or came back empty.
    """
    lists = await asyncio.gather(*(
        asyncio.to_thread(_fetch_list, list_id, market) for list_id in GAME_PASS_LIST_IDS
    ))
    if any(not ids for ids in lists):
        logger.warning(
            "[GamePass] could not read every Game Pass list (market=%s); "
            "the Steam Store ribbon cannot say which Xbox titles are Game Pass",
            market,
        )
        return None
    listed = frozenset().union(*lists)
    logger.info("[GamePass] %d products on the Game Pass lists (market=%s)", len(listed), market)
    return listed


def membership(
    product_ids: Iterable[str],
    products: Mapping[str, Mapping[str, Any]],
    listed: frozenset[str],
) -> dict[str, bool]:
    """UPPER-case product id → whether it is in the Game Pass catalog.

    *products* are raw displaycatalog products by UPPER-case id; a product
    missing from it can only match by its own id.
    """
    answer: dict[str, bool] = {}
    for pid in product_ids:
        key = pid.upper()
        raw = products.get(key) or {}
        answer[key] = key in listed or not listed.isdisjoint(related_product_ids(raw))
    return answer


def related_product_ids(raw: Mapping[str, Any]) -> set[str]:
    """The bundles and editions a displaycatalog product is sold through."""
    markets = raw.get("MarketProperties")
    if not isinstance(markets, list) or not markets or not isinstance(markets[0], dict):
        return set()
    related = markets[0].get("RelatedProducts")
    if not isinstance(related, list):
        return set()
    return {
        r["RelatedProductId"].upper()
        for r in related
        if isinstance(r, dict)
        and r.get("RelationshipType") in _RELATED_TYPES
        and isinstance(r.get("RelatedProductId"), str)
    }


def _fetch_list(list_id: str, market: str) -> frozenset[str]:
    """One Game Pass list's product ids; empty when it cannot be read."""
    query = urllib.parse.urlencode({"id": list_id, "language": "en-us", "market": market})
    request = urllib.request.Request(
        f"{_SIGLS_URL}?{query}", headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=20,
            context=_ssl("Game Pass catalog lists — outdated Deck cert store"),
        ) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("[GamePass] list %s unreadable: %s", list_id, type(e).__name__)
        return frozenset()
    return parse_list(data)


def parse_list(data: Any) -> frozenset[str]:
    """Product ids from a sigls answer: a header object, then ``{"id": ...}`` rows."""
    if not isinstance(data, list):
        return frozenset()
    return frozenset(
        row["id"].upper()
        for row in data
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    )
