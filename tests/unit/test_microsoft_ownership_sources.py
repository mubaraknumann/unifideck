"""The three Xbox ownership sources, against shapes measured on a real account.

- Collections: owned = Game ∧ Active ∧ not trial ∧ not a pass;
  ``licensingRequired`` = Games with Gold. Paging until no token; any
  non-200 page or a runaway page count is a failed read, never a partial list.
- displaycatalog: PC / console / Play Anywhere from packages, gens and XPA;
  a bundle takes its parts' platforms; add-ons filed as games are dropped.
- Xbox 360 back-compat: candidates from charged orders and 360 play history,
  each confirmed by productActions, where ``BuyToOwn`` at the product or the
  sku level, or ``NotSoldSeparately``, means not owned.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.stores.microsoft.ownership import backcompat, catalog_platforms, collections_api
from unifideck.stores.microsoft.ownership.collections_api import ItemKind, classify
from unifideck.stores.microsoft.tokens.endpoint import HttpReply


def _item(**kw: Any) -> dict[str, Any]:
    base = {"productId": "9PNQKHFLD2WQ", "productKind": "Game", "status": "Active", "skuType": "Full"}
    return {**base, **kw}


@pytest.mark.parametrize(("item", "kind"), [
    (_item(), ItemKind.OWNED),
    (_item(licensingRequired=True), ItemKind.GOLD),
    (_item(licensingRequired=None), ItemKind.OWNED),
    (_item(status="Expired"), ItemKind.INACTIVE),
    (_item(skuType="Trial"), ItemKind.TRIAL),
    (_item(isTrial=True), ItemKind.TRIAL),
    (_item(productKind="Durable"), ItemKind.NOT_GAME),
    (_item(productId="CFQ7TTC0KHS0", productKind="Pass"), ItemKind.SUBSCRIPTION),
    (_item(productId=""), ItemKind.MALFORMED),
])
def test_classify(item: dict[str, Any], kind: ItemKind) -> None:
    assert classify(item) is kind


def test_collections_pages_until_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        HttpReply(200, {"items": [_item(productId="A")], "continuationToken": "t1"}),
        HttpReply(200, {"items": [_item(productId="B")]}),
    ]
    sent: list[dict[str, Any]] = []

    def fake(method: str, url: str, headers: dict[str, str], payload: Any = None, timeout: float = 20.0) -> HttpReply:
        sent.append(dict(payload))
        return pages.pop(0)

    monkeypatch.setattr(collections_api, "request_json", fake)
    result = collections_api.query_collections("uhs", "xsts", "123")
    assert result.ok and [i["productId"] for i in result.items] == ["A", "B"]
    assert sent[0]["beneficiaries"][0] == {"identityType": "xuid", "identityValue": "123",
                                           "localTicketReference": "unifideck"}
    assert sent[1]["continuationToken"] == "t1"


@pytest.mark.parametrize("reply", [HttpReply(401, {}), HttpReply(None, detail="offline"), HttpReply(200, {})])
def test_a_failed_page_is_a_failed_read(monkeypatch: pytest.MonkeyPatch, reply: HttpReply) -> None:
    monkeypatch.setattr(collections_api, "request_json", lambda *a, **k: reply)
    result = collections_api.query_collections("uhs", "xsts", "123")
    assert result.ok is False and result.items == ()
    assert "collections.mp.microsoft.com" in result.reason


def test_runaway_paging_is_a_failed_read(monkeypatch: pytest.MonkeyPatch) -> None:
    endless = HttpReply(200, {"items": [_item()], "continuationToken": "again"})
    monkeypatch.setattr(collections_api, "request_json", lambda *a, **k: endless)
    assert collections_api.query_collections("uhs", "xsts", "123").ok is False


# ── displaycatalog platforms ─────────────────────────────────────────

def _product(pid: str, title: str, *, deps: tuple[str, ...] = (), xpa: bool = False,
             gens: tuple[str, ...] = (), bundled: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "ProductId": pid,
        "LocalizedProperties": [{"ProductTitle": title}],
        "Properties": {"XboxConsoleGenCompatible": list(gens), "Attributes": []},
        "DisplaySkuAvailabilities": [{"Sku": {"Properties": {
            "XboxXPA": xpa,
            "Packages": [{"PlatformDependencies": [{"PlatformName": d} for d in deps]}],
            "BundledSkus": [{"BigId": b} for b in bundled],
        }}}],
    }


async def test_platforms_and_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = {
        "FH4": _product("FH4", "Forza Horizon 4", deps=("Windows.Desktop", "Windows.Xbox"), xpa=True),
        "SEKIRO": _product("SEKIRO", "Sekiro", deps=("Windows.Xbox",)),
        "AWAN": _product("AWAN", "Alan Wake's American Nightmare", deps=("Windows.Desktop",)),
        "DOOM": _product("DOOM", "DOOM Eternal Standard Edition (PC)", bundled=("BATTLE",)),
        "BATTLE": _product("BATTLE", "DOOM Eternal (BATTLEMODE - PC)", deps=("Windows.Desktop",)),
        "GEN": _product("GEN", "Titanfall 2", gens=("ConsoleGen8",)),
    }

    async def fake_fetch(ids: list[str], market: str) -> dict[str, dict[str, Any]]:
        return {i: catalog[i] for i in ids if i in catalog}

    monkeypatch.setattr(catalog_platforms, "fetch_products", fake_fetch)
    info = await catalog_platforms.describe_products(["FH4", "SEKIRO", "AWAN", "DOOM", "GEN"])
    assert (info["FH4"].pc, info["FH4"].xbox, info["FH4"].xpa) == (True, True, True)
    assert (info["SEKIRO"].pc, info["SEKIRO"].xbox) == (False, True)
    assert (info["AWAN"].pc, info["AWAN"].xbox) == (True, False)
    assert (info["DOOM"].pc, info["DOOM"].xbox) == (True, False)  # from its part
    assert info["GEN"].xbox is True


@pytest.mark.parametrize(("title", "extra"), [
    ("Starfield Premium Edition Upgrade", True),
    ("Starfield Digital Artbook & Original Soundtrack", True),
    ("Dead Space - Add-On Bundle 2", True),
    ("REANIMAL: Friend's Pass", True),
    ("Resident Evil Village Gold Edition Gameplay Demo", True),
    ("Titanfall® 2: Ultimate Edition", False),
    ("Mafia: Trilogy", False),
])
def test_add_ons_filed_as_games(title: str, extra: bool) -> None:
    assert catalog_platforms.looks_like_extra(title) is extra


# ── Xbox 360 back-compat ─────────────────────────────────────────────

def _actions(product: list[str], sku: list[str] | None = None) -> dict[str, Any]:
    return {"productActions": [{
        "productActions": [{"actionType": a} for a in product],
        "skuActionsBySkuId": {"0010": [{"actionType": a} for a in sku or []]},
    }]}


@pytest.mark.parametrize(("payload", "owned"), [
    (_actions(["Install", "RedeemACode", "Wishlist"]), True),               # METAL GEAR RISING
    (_actions(["Install", "Gift"], ["BuyToOwn", "Cart"]), False),           # Game Pass, sku level
    (_actions(["Install", "BuyToOwn"]), False),                             # Game Pass, product level
    (_actions(["Install", "NotSoldSeparately"]), False),                    # EA Play bundle title
    (_actions(["Gift"], ["Acquisition", "Cart"]), False),                   # for sale, not owned
    ({}, False),
])
def test_actions_verdict(payload: dict[str, Any], owned: bool) -> None:
    assert backcompat.actions_owned(payload) is owned


def test_candidates_from_orders_and_history() -> None:
    orders = [
        {"orderState": "Purchased", "orderLineItems": [
            {"productId": "br6hqdnqt004", "billingState": "Charged", "productType": "Game", "title": "MGR"},
            {"productId": "DLC1", "billingState": "Charged", "productType": "Durable", "title": "DLC"}]},
        {"orderState": "CheckingOut", "orderLineItems": [
            {"productId": "CART1", "billingState": "Charged", "title": "Cart"}]},
    ]
    history = [
        {"name": "Fallout 3", "devices": ["Xbox360", "XboxOne"],
         "detail": {"availabilities": [{"ProductId": "C29HQ887KH4B"}]}},
        {"name": "Sekiro", "devices": ["XboxOne"], "detail": {"availabilities": [{"ProductId": "X"}]}},
    ]
    assert backcompat.order_candidates(orders) == {"BR6HQDNQT004": "MGR"}
    assert backcompat.history_candidates(history) == {"C29HQ887KH4B": "Fallout 3"}


async def test_backcompat_checks_only_unknown_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backcompat, "fetch_orders", lambda *a: [
        {"orderState": "Purchased", "orderLineItems": [
            {"productId": "MGR", "billingState": "Charged", "title": "MGR"},
            {"productId": "KNOWN", "billingState": "Charged", "title": "Already in Collections"}]}])
    monkeypatch.setattr(backcompat, "fetch_history", lambda *a: [])
    checked: list[str] = []

    def check(uhs: str, xsts: str, pid: str) -> bool:
        checked.append(pid)
        return pid == "MGR"

    monkeypatch.setattr(backcompat, "check_owned", check)
    monkeypatch.setattr(backcompat, "_CHECK_PAUSE_SECONDS", 0)
    owned = await backcompat.find_backcompat_owned("u", "s", "l", "1", known={"KNOWN"})
    assert owned == {"MGR": "MGR"}
    assert checked == ["MGR"]
