"""Which xCloud titles are in the Game Pass catalog.

``/v2/titles`` marks Game Pass titles and owned games that stream the same
way, so the ribbon reads the public Game Pass lists to tell them apart.
Measured 2026-10-04: the lists often name a bundle, not the product xCloud
streams (Forza Horizon 5 streams as ``9NNX1VVR3KNQ``, Game Pass lists the
bundle ``9NKX70BBCDRN``), so a related product counts. A list that cannot be
read makes the answer unknown, never "not in Game Pass".
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.core.cross_store_ownership import GAME_PASS_KEY
from unifideck.stores.microsoft import game_pass as gp
from unifideck.stores.microsoft.microsoft_catalog import MicrosoftCatalogReader

FH5 = "9NNX1VVR3KNQ"
FH5_BUNDLE = "9NKX70BBCDRN"
MAFIA = "9NZC09NNR93L"


def _product(*related: tuple[str, str]) -> dict[str, Any]:
    return {"MarketProperties": [{"RelatedProducts": [
        {"RelatedProductId": pid, "RelationshipType": kind} for pid, kind in related
    ]}]}


def test_a_list_is_its_id_rows_after_the_header() -> None:
    data = [{"siglId": "x", "title": "All games"}, {"id": "9nkx70bbcdrn"}, {"id": ""}, "junk"]
    assert gp.parse_list(data) == frozenset({FH5_BUNDLE})
    assert gp.parse_list({"not": "a list"}) == frozenset()


def test_only_bundle_and_sellable_by_relations_count() -> None:
    raw = _product((FH5_BUNDLE, "Bundle"), ("SELLER", "SellableBy"), ("ADDON", "AddOn"))
    assert gp.related_product_ids(raw) == {FH5_BUNDLE, "SELLER"}
    assert gp.related_product_ids({}) == set()
    assert gp.related_product_ids({"MarketProperties": [{"RelatedProducts": "bad"}]}) == set()


def test_membership_by_own_id_or_by_a_related_bundle() -> None:
    listed = frozenset({FH5_BUNDLE, "LISTED"})
    products = {FH5: _product((FH5_BUNDLE, "Bundle")), MAFIA: _product(("OTHER", "Bundle"))}
    assert gp.membership(["listed", FH5, MAFIA, "UNKNOWN"], products, listed) == {
        "LISTED": True, FH5: True, MAFIA: False, "UNKNOWN": False,
    }


async def test_every_list_must_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "_fetch_list", lambda list_id, market: frozenset({list_id.upper()}))
    assert await gp.fetch_game_pass_ids("IN") == frozenset(i.upper() for i in gp.GAME_PASS_LIST_IDS)

    broken = gp.GAME_PASS_LIST_IDS[2]
    monkeypatch.setattr(
        gp, "_fetch_list",
        lambda list_id, market: frozenset() if list_id == broken else frozenset({"X"}),
    )
    assert await gp.fetch_game_pass_ids("IN") is None


def test_rows_carry_the_flag_only_when_it_is_known() -> None:
    entitled = [{"details": {"productId": FH5}}, {"details": {"productId": MAFIA.lower()}}]

    known = MicrosoftCatalogReader._build_xcloud_games(entitled, {}, {FH5: True, MAFIA: False})
    assert [g.metadata[GAME_PASS_KEY] for g in known] == [True, False]

    unknown = MicrosoftCatalogReader._build_xcloud_games(entitled, {}, None)
    assert all(GAME_PASS_KEY not in g.metadata for g in unknown)
