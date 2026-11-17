"""The DLC the account owns for one Xbox PC game, answered when the game page asks.

Not part of the library sync: the game page loads it with the rest of its
details (``get_xbox_pc_dlc``), and the install reads the same answer, so
the picker and the download always agree.

1. **Owned products:** :meth:`XboxPcLibrary.owned_ids`, the ids with
   install access (owned, Game Pass) of the last ``library`` read when it
   is under ``ENTITLEMENTS_MAX_AGE_S`` old, else of a new read. When the
   sign-in is busy (a sync or a game holds it) it answers at once with
   the ids of the last read, kept across restarts, so a page load or an
   install never waits on it.
2. **Which are DLC of this game:** their catalog records, read in batches
   and kept ``PRODUCT_TTL_S``. A product the cached plan summaries already
   know is not a ``Durable`` is never fetched, so a library of hundreds of
   games costs one read of its add-ons. A pass (a ``Durable`` with no package
   of its own) is replaced by the add-ons it grants (``dlc.bundle_members``).
3. Keep the DLC whose parent is the game's target or entitlement id.

A catalog batch that fails leaves those products out of this answer
without caching the gap, so the next page load asks again.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from unifideck.stores.microsoft.microsoft_catalog import fetch_products_detailed

from .dlc import DlcInfo, bundle_members, dlc_of
from .entitlements import is_product_id
from .package_plan import CATALOG_LANGUAGES, FetchDetailed

logger = logging.getLogger(__name__)

#: How old the entitlement list may be before a page load reads it again.
ENTITLEMENTS_MAX_AGE_S = 300.0
#: How long one catalog record's DLC facts are kept.
PRODUCT_TTL_S = 24 * 3600.0


class LibraryLike(Protocol):
    """What this needs from ``XboxPcLibrary``."""

    def title_for(self, store_game_id: str) -> dict[str, Any] | None: ...

    def resolve_market(self, market: str | None) -> str: ...

    def known_kinds(self, market: str) -> dict[str, str]: ...

    async def owned_ids(
        self, market: str | None, *, max_age_s: float, force: bool = False,
    ) -> list[str] | None: ...


@dataclass(frozen=True)
class _Facts:
    """What one catalog record says: a DLC, a pass, or neither."""

    at: float
    dlc: DlcInfo | None = None
    members: tuple[str, ...] = ()


@dataclass
class _Round:
    facts: dict[str, _Facts] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)


class OwnedDlc:
    """Owned, installable DLC per game, on demand."""

    def __init__(
        self, library: LibraryLike, *,
        fetch: FetchDetailed = fetch_products_detailed,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._library = library
        self._fetch = fetch
        self._clock = clock
        self._facts: dict[str, _Facts] = {}
        #: One catalog read at a time: page loads at once share its answer.
        self._fetch_lock = asyncio.Lock()

    async def for_game(self, game_id: str, *, force: bool = False) -> list[DlcInfo] | None:
        """Owned DLC of ``game_id``, sorted by title.

        None when ``game_id`` is not an Xbox PC title or the account's
        entitlements cannot be read at all; [] when it owns none.
        """
        meta = self._library.title_for(game_id)
        if not meta:
            return None
        parents = {str(meta.get(k) or "").upper() for k in ("target_id", "product_id")}
        parents.discard("")
        market = self._library.resolve_market(meta.get("market"))
        ids = await self._library.owned_ids(
            market, max_age_s=ENTITLEMENTS_MAX_AGE_S, force=force,
        )
        if ids is None:
            return None
        owned = self._owned_candidates(ids, market)
        found = await self._dlc_of(owned, market, force)
        mine = {d.product_id: d for d in found if d.parent in parents}
        return sorted(mine.values(), key=lambda d: (d.title.lower(), d.product_id))

    def _owned_candidates(self, ids: list[str], market: str) -> list[str]:
        """Owned product ids that may be DLC: not known to be another kind."""
        kinds = self._library.known_kinds(market)
        return sorted(
            pid for pid in ids
            if is_product_id(pid) and kinds.get(pid, "Durable") == "Durable"
        )

    async def _dlc_of(self, owned: list[str], market: str, force: bool) -> list[DlcInfo]:
        """The DLC among ``owned``, passes replaced by their members."""
        async with self._fetch_lock:
            first = await self._facts_for(owned, market, force)
            members = sorted({m for f in first.facts.values() for m in f.members} - set(owned))
            second = await self._facts_for(members, market, force)
        facts = {**first.facts, **second.facts}
        if first.failed or second.failed:
            logger.warning(
                "[OwnedDlc] %d catalog record(s) could not be read; their DLC is left out until the next page load",
                len(first.failed | second.failed),
            )
        out: list[DlcInfo] = []
        for pid in owned:
            fact = facts.get(pid)
            if fact is None:
                continue
            if fact.dlc is not None:
                out.append(fact.dlc)
            out.extend(f.dlc for m in fact.members if (f := facts.get(m)) and f.dlc)
        return out

    async def _facts_for(self, ids: list[str], market: str, force: bool) -> _Round:
        """Catalog facts of ``ids``: cached ones, plus one batched read of the rest."""
        now = self._clock()
        result = _Round()
        missing: list[str] = []
        for pid in ids:
            cached = self._facts.get(pid)
            if cached is not None and not force and now - cached.at < PRODUCT_TTL_S:
                result.facts[pid] = cached
            else:
                missing.append(pid)
        if not missing:
            return result
        try:
            products, failed = await self._fetch(missing, market, languages=CATALOG_LANGUAGES)
        except Exception as e:
            logger.warning("[OwnedDlc] catalog read failed: %s", type(e).__name__)
            result.failed.update(missing)
            return result
        result.failed.update(failed)
        for pid in missing:
            if pid in result.failed:
                continue
            product = products.get(pid)
            fact = _Facts(at=now, dlc=dlc_of(product, pid),
                          members=tuple(bundle_members(product, pid)))
            self._facts[pid] = result.facts[pid] = fact
        return result
