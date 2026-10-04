"""Microsoft Store Collections: the products a Microsoft account holds.

``POST collections.mp.microsoft.com/v7.0/collections/query`` with an XSTS
token for ``http://mp.microsoft.com/``. Undocumented for consumers; what is
pinned here was measured on a real account on 2026-10-02 and matched the
console's own "Owned games" and "Games with Gold" tabs title for title:

- Results are partitioned by the calling app: the login.live.com client
  Unifideck used up to 0.7.5 gets ``[]``; xbox.com's client gets the real
  library. Hence the 0.7.6 sign-in switch.
- Owned = ``productKind`` Game, ``status`` Active, not a trial, not a
  ``CFQ7TTC0…`` pass.
- ``licensingRequired: true`` marks Games with Gold grants: playable only
  while a qualifying subscription is active. ``acquisitionType`` is always
  null on v7, so it cannot tell them apart.
- Game Pass titles are absent (no ``expandSatisfyingItems``), and so are
  Xbox 360 backward-compatible purchases (see ``backcompat``).

A partial list is never returned: the reader keeps the last good index
rather than act on half a library.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from unifideck.stores.microsoft.tokens.endpoint import request_json

COLLECTIONS_URL = "https://collections.mp.microsoft.com/v7.0/collections/query"
_PAGE_SIZE = 100
#: A real library is a few hundred items; Microsoft documents ~100 queries
#: per 5 minutes per user for this service.
_MAX_PAGES = 30
_SUBSCRIPTION_ID_PREFIX = "CFQ7TTC0"


class ItemKind(enum.Enum):
    """What one Collections item means for ownership."""

    OWNED = "owned"
    GOLD = "gold"
    """Games with Gold: playable while subscribed."""
    INACTIVE = "inactive"
    NOT_GAME = "not_game"
    SUBSCRIPTION = "subscription"
    """A Game Pass / Gold subscription item, not a game."""
    TRIAL = "trial"
    MALFORMED = "malformed"


def classify(item: dict[str, Any]) -> ItemKind:
    """Sort one Collections item."""
    pid = item.get("productId")
    if not isinstance(pid, str) or not pid:
        return ItemKind.MALFORMED
    if pid.upper().startswith(_SUBSCRIPTION_ID_PREFIX) or item.get("productKind") == "Pass":
        return ItemKind.SUBSCRIPTION
    if item.get("productKind") != "Game":
        return ItemKind.NOT_GAME
    if item.get("status") != "Active":
        return ItemKind.INACTIVE
    if item.get("isTrial") is True or item.get("skuType") == "Trial":
        return ItemKind.TRIAL
    if item.get("licensingRequired") is True:
        return ItemKind.GOLD
    return ItemKind.OWNED


@dataclass(frozen=True)
class CollectionsResult:
    """Every item, or why there is no list (``reason`` names host and page)."""

    ok: bool
    reason: str = ""
    items: tuple[dict[str, Any], ...] = ()


def query_collections(
    user_hash: str, xsts_token: str, xuid: str, market: str = "US",
) -> CollectionsResult:
    """Page through the account's whole collection. Blocking; never raises."""
    body: dict[str, Any] = {
        "maxPageSize": _PAGE_SIZE,
        "excludeDuplicates": True,
        "market": market,
        "validityType": "All",
        "beneficiaries": [{
            "identityType": "xuid", "identityValue": xuid,
            "localTicketReference": "unifideck",
        }],
    }
    items: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        reply = request_json("POST", COLLECTIONS_URL, {
            "Authorization": f"XBL3.0 x={user_hash};{xsts_token}",
            "MS-CV": f"{uuid.uuid4().hex.upper()}.0",
        }, body)
        got = reply.body.get("items")
        if reply.status != 200 or not isinstance(got, list):
            status = reply.status if reply.status is not None else reply.detail
            return CollectionsResult(
                ok=False,
                reason=f"collections.mp.microsoft.com answered {status} on page {page}",
            )
        items.extend(i for i in got if isinstance(i, dict))
        token = reply.body.get("continuationToken")
        if not token or not got:
            return CollectionsResult(ok=True, items=tuple(items))
        body["continuationToken"] = token
    return CollectionsResult(
        ok=False, reason=f"collections.mp.microsoft.com sent more than {_MAX_PAGES} pages",
    )
