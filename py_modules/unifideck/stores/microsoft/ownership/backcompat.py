"""Xbox 360 backward-compatible purchases, which Collections never lists.

A 360-era title bought in the modern store (METAL GEAR RISING: REVENGEANCE,
a paid order from 2022 on the account this was measured on) is licensed by
the old system and absent from Collections. It shows up in two places that
can be listed: the account's charged orders, and played-title history with
``Xbox360`` among the devices. Neither proves ownership on its own (orders
can be refunded; history includes Game Pass and borrowed games), so every
candidate is checked against xbox.com's own "you own this" answer.

That check (``emerald.xboxservices.com/xboxcomfd/productActions/{pid}``)
was validated 12/12 against the user's ground truth, with two traps:

- ``BuyToOwn`` can sit on the sku, not the product: either place means
  "playable through Game Pass, not owned";
- ``NotSoldSeparately`` + ``Install`` is a subscription bundle title (EA
  Play, Ubisoft+, first party): it cannot be judged, so it is not owned.

Known gap: a 360 title with no order and never played on this account (an
old 360 Games with Gold claim) cannot be found by anything listable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.stores.microsoft.tokens.endpoint import request_json

logger = logging.getLogger(__name__)

ORDERS_URL = "https://purchase.mp.microsoft.com/v7.0/users/me/orders"
TITLEHUB_URL = (
    "https://titlehub.xboxlive.com/users/xuid({xuid})/titles/titlehistory/decoration/detail"
)
ACTIONS_URL = "https://emerald.xboxservices.com/xboxcomfd/productActions/{pid}?locale=en-US"
#: One request per candidate; the user's own account had one real hit.
MAX_CHECKS = 30
_MAX_ORDER_PAGES = 50
_CHECK_PAUSE_SECONDS = 0.3


def actions_owned(payload: dict[str, Any]) -> bool:
    """xbox.com's verdict from a productActions answer: True only if owned."""
    entries = payload.get("productActions")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return False
    entry = entries[0]
    actions = {a.get("actionType") for a in entry.get("productActions") or () if isinstance(a, dict)}
    for sku_actions in (entry.get("skuActionsBySkuId") or {}).values():
        actions |= {a.get("actionType") for a in sku_actions or () if isinstance(a, dict)}
    if "BuyToOwn" in actions or "NotSoldSeparately" in actions:
        return False
    return "Install" in actions


def order_candidates(orders: list[dict[str, Any]]) -> dict[str, str]:
    """Charged game lines of placed orders: product id → title."""
    out: dict[str, str] = {}
    for order in orders:
        if order.get("orderState") not in ("Purchased", "Refunded"):
            continue
        for line in order.get("orderLineItems") or ():
            if _is_charged_game(line):
                out.setdefault(line["productId"].upper(), str(line.get("title") or ""))
    return out


def _is_charged_game(line: Any) -> bool:
    if not isinstance(line, dict) or not isinstance(line.get("productId"), str):
        return False
    kind = str(line.get("productType") or "Game").lower()
    return line.get("billingState") == "Charged" and kind == "game"


def history_candidates(titles: list[dict[str, Any]]) -> dict[str, str]:
    """Titles played on an Xbox 360: product id → name."""
    out: dict[str, str] = {}
    for title in titles:
        if "Xbox360" not in (title.get("devices") or ()):
            continue
        for pid in _product_ids(title.get("detail")):
            out.setdefault(pid, str(title.get("name") or ""))
    return out


def _product_ids(detail: Any) -> list[str]:
    if not isinstance(detail, dict):
        return []
    return [
        a["ProductId"].upper() for a in detail.get("availabilities") or ()
        if isinstance(a, dict) and isinstance(a.get("ProductId"), str) and a["ProductId"]
    ]


def fetch_orders(user_hash: str, store_xsts: str) -> list[dict[str, Any]] | None:
    """Every order on the account (``@nextLink`` paging), or None on failure."""
    orders: list[dict[str, Any]] = []
    url: str | None = ORDERS_URL  # no query string: a market= parameter is a 400
    for _ in range(_MAX_ORDER_PAGES):
        if url is None:
            return orders
        reply = request_json("GET", url, _auth(user_hash, store_xsts))
        items = reply.body.get("items")
        if reply.status != 200 or not isinstance(items, list):
            return None
        orders.extend(o for o in items if isinstance(o, dict))
        nxt = reply.body.get("@nextLink")
        url = nxt if isinstance(nxt, str) and nxt else None
    return orders


def fetch_history(user_hash: str, live_xsts: str, xuid: str) -> list[dict[str, Any]] | None:
    """Played-title history, or None on failure."""
    reply = request_json(
        "GET", TITLEHUB_URL.format(xuid=xuid) + "?maxItems=5000",
        {**_auth(user_hash, live_xsts), "x-xbl-contract-version": "2", "Accept-Language": "en-US"},
    )
    titles = reply.body.get("titles")
    return [t for t in titles if isinstance(t, dict)] if reply.status == 200 and isinstance(titles, list) else None


def check_owned(user_hash: str, store_xsts: str, pid: str) -> bool:
    """xbox.com's own "you own this" for one product."""
    reply = request_json(
        "GET", ACTIONS_URL.format(pid=pid),
        {**_auth(user_hash, store_xsts), "x-ms-api-version": "1.0"},
    )
    return reply.status == 200 and actions_owned(reply.body)


async def find_backcompat_owned(
    user_hash: str, store_xsts: str, live_xsts: str, xuid: str, known: set[str],
) -> dict[str, str]:
    """Owned products outside Collections: product id → title. Best effort."""
    orders = await asyncio.to_thread(fetch_orders, user_hash, store_xsts)
    history = await asyncio.to_thread(fetch_history, user_hash, live_xsts, xuid)
    if orders is None or history is None:
        logger.warning(
            "[MicrosoftOwnership] could not read %s; Xbox 360 purchases outside "
            "Collections are not checked this time",
            "orders (purchase.mp.microsoft.com)" if orders is None
            else "play history (titlehub.xboxlive.com)",
        )
    candidates = {**history_candidates(history or []), **order_candidates(orders or [])}
    todo = [pid for pid in candidates if pid not in known][:MAX_CHECKS]
    owned: dict[str, str] = {}
    for pid in todo:
        if await asyncio.to_thread(check_owned, user_hash, store_xsts, pid):
            owned[pid] = candidates[pid]
        await asyncio.sleep(_CHECK_PAUSE_SECONDS)
    return owned


def _auth(user_hash: str, xsts: str) -> dict[str, str]:
    return {"Authorization": f"XBL3.0 x={user_hash};{xsts}"}
