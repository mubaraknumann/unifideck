"""Read the Xbox products a Microsoft account owns, with where each one plays.

One pass, all of it read-only:

1. XSTS for ``xboxlive.com`` (carries the xuid) and, from the same XBL user
   token, for ``mp.microsoft.com`` (the Store's services);
2. Collections → owned games and Games with Gold grants (``collections_api``);
3. displaycatalog → title and PC / Xbox / Play Anywhere (``catalog_platforms``),
   dropping demos and add-ons filed as games;
4. best effort: Xbox 360 purchases Collections omits (``backcompat``).

Steps 1–2 failing is a failed read (``OwnedFetch.ok`` False with a reason
that names the host): the caller keeps its last good list. Steps 3–4
failing only costs detail: a product with no catalog entry keeps an empty
title and no platform, and a missed 360 check is retried next time.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .backcompat import find_backcompat_owned
from .catalog_platforms import ProductInfo, describe_products, looks_like_extra
from .collections_api import ItemKind, classify, query_collections

if TYPE_CHECKING:
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager, XBLTokenChain

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnedProduct:
    """One product the account owns."""

    title: str
    pc: bool
    xbox: bool
    xpa: bool
    gold: bool
    """Games with Gold: playable only while a qualifying subscription lasts."""
    source: str
    """``collections`` or ``backcompat``."""


@dataclass(frozen=True)
class OwnedFetch:
    """The owned products, or why they could not be read."""

    ok: bool
    reason: str = ""
    products: dict[str, OwnedProduct] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    @classmethod
    def failed(cls, reason: str) -> OwnedFetch:
        """A read that produced no list."""
        return cls(ok=False, reason=reason)


class MicrosoftOwnershipReader:
    """Reads ownership with the store's shared token manager."""

    def __init__(self, tokens: MicrosoftTokenManager) -> None:
        """Initialize the instance."""
        self._tokens = tokens

    async def fetch(self) -> OwnedFetch:
        """Every owned product. Never raises."""
        chains = await self._chains()
        if isinstance(chains, str):
            return OwnedFetch.failed(chains)
        live, store = chains
        result = await asyncio.to_thread(
            query_collections, store.user_hash, store.xsts_token, live.xuid or "",
        )
        if not result.ok:
            return OwnedFetch.failed(result.reason)
        kinds = {
            str(item["productId"]).upper(): classify(item)
            for item in result.items if classify(item) is not ItemKind.MALFORMED
        }
        products = await _describe_owned(kinds)
        extra = await find_backcompat_owned(
            store.user_hash, store.xsts_token, live.xsts_token, live.xuid or "",
            known=set(kinds),
        )
        for pid, title in extra.items():
            products[pid] = OwnedProduct(title, False, True, False, False, "backcompat")
        stats = dict(Counter(kind.value for kind in kinds.values()))
        stats["backcompat"] = len(extra)
        return OwnedFetch(ok=True, products=products, stats=stats)

    async def _chains(self) -> tuple[XBLTokenChain, XBLTokenChain] | str:
        """The xuid-bearing chain and the Store chain, or why not."""
        live = await self._tokens.build_chain()
        if live is None or not live.xuid:
            return "xsts.auth.xboxlive.com refused the Xbox Live token"
        store = await self._tokens.build_marketplace_chain(live.xbl_token)
        if store is None:
            return "xsts.auth.xboxlive.com refused the Microsoft Store token"
        return live, store


async def _describe_owned(kinds: dict[str, ItemKind]) -> dict[str, OwnedProduct]:
    wanted = [pid for pid, k in kinds.items() if k in (ItemKind.OWNED, ItemKind.GOLD)]
    info = await describe_products(wanted)
    products: dict[str, OwnedProduct] = {}
    for pid in wanted:
        product = info.get(pid, ProductInfo(title=""))
        if product.demo or looks_like_extra(product.title):
            continue
        products[pid] = OwnedProduct(
            title=product.title,
            pc=product.pc,
            xbox=product.xbox,
            xpa=product.xpa,
            gold=kinds[pid] is ItemKind.GOLD,
            source="collections",
        )
    return products
