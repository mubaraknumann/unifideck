"""Free-to-play titles are granted on a presumption, and nothing is lost.

The PUB catalog gates free-to-play and subscription titles on
``game_account`` rather than ``license_id``, and no client-local file
records which game accounts a user holds: ``CachedData.db`` carries licences
and a battle tag, nothing more. That cost 7 of 24 titles on a real account
(WoW, Hearthstone, Overwatch, Heroes of the Storm, Diablo Immortal and two
more) for as long as the store read a ``game_accounts`` cache key that
nothing ever wrote.

``grant_ownership`` presumes a game account for every catalog program
instead. The risk of a presumption is that it *changes* an answer the
licences already gave, so most of what follows pins the other direction:
adding program keys can never remove or alter a title the account really
owns. That is a property of the merge, and these tests are what keep it one.
"""
from __future__ import annotations

from typing import Any

from unifideck.stores.battlenet.library import grant_ownership
from unifideck.stores.battlenet.ownership import AccountFacts, evaluate_catalog

# One licence-gated title and one game-account-gated title. Shaped like the
# real PUB catalog: ``match`` against account facts, ``add_product`` action.
_CATALOG: dict[str, Any] = {
    "D3": {
        "run_each_rule": [
            {
                "match": {"license_id": 1},
                "actions": [
                    {"add_product": {"product_id": {"id": "d3", "type": "retail"}}},
                ],
            },
        ],
    },
    "WTCG": {
        "run_each_rule": [
            {
                "match": {"game_account": {"program_id": "WTCG"}},
                "actions": [
                    {"add_product": {"product_id": {"id": "hs", "type": "retail"}}},
                    {"add_tag": {"name": "play_for_free"}},
                ],
            },
        ],
    },
}


class _Catalog:
    """Minimal stand-in for ``MergedCatalog``'s one field used here."""

    def __init__(self, configs: dict[str, Any]) -> None:
        self.program_configurations = configs


def test_a_game_account_gated_title_is_granted_presumptively() -> None:
    """The free title is the whole point: licences alone never reach it."""
    facts = AccountFacts(licence_ids=frozenset({1}))
    granted, presumed = grant_ownership(_Catalog(_CATALOG), facts)
    assert set(granted) == {"D3", "WTCG"}
    assert presumed == frozenset({"WTCG"})
    assert any(p.is_free_to_play for p in granted["WTCG"])


def test_the_licence_granted_entry_is_returned_unchanged() -> None:
    """An owned title must come back byte-identical, tags included.

    Merging the probe's products into an already-granted program would let
    a presumed ``play_for_free`` tag land on a game the user bought.
    """
    facts = AccountFacts(licence_ids=frozenset({1}))
    granted, presumed = grant_ownership(_Catalog(_CATALOG), facts)
    assert granted["D3"] == evaluate_catalog(_CATALOG, facts)["D3"]
    assert "D3" not in presumed
    assert not any(p.is_free_to_play for p in granted["D3"])


def test_a_run_first_branch_is_not_reopened_for_an_already_granted_program() -> None:
    """First-match-wins stays decided by the licences, not by the probe."""
    catalog = {
        "X": {
            "run_first_rule": [
                {
                    "match": {"license_id": 1},
                    "actions": [
                        {"add_product": {"product_id": {"id": "x", "type": "retail"}}},
                    ],
                },
                {
                    "match": {"game_account": {"program_id": "X"}},
                    "actions": [
                        {"add_product": {"product_id": {"id": "x", "type": "gamepass"}}},
                    ],
                },
            ],
        },
    }
    granted, presumed = grant_ownership(_Catalog(catalog), AccountFacts(licence_ids=frozenset({1})))
    assert {p.product_type for p in granted["X"]} == {"retail"}
    assert "X" not in presumed


def test_a_not_game_account_rule_keeps_its_grant() -> None:
    """The one shape the probe flips to False must not cost a title."""
    catalog = {
        "N": {
            "run_each_rule": [
                {
                    "match": {"not": {"game_account": {"program_id": "WTCG"}}},
                    "actions": [
                        {"add_product": {"product_id": {"id": "n", "type": "retail"}}},
                    ],
                },
            ],
        },
    }
    granted, presumed = grant_ownership(_Catalog(catalog), AccountFacts())
    assert "N" in granted
    assert presumed == frozenset()


def test_real_game_account_facts_skip_the_probe() -> None:
    """If a producer ever lands, its facts win and nothing is presumed."""
    facts = AccountFacts(
        licence_ids=frozenset({1}),
        game_account_programs=frozenset({"WTCG"}),
    )
    granted, presumed = grant_ownership(_Catalog(_CATALOG), facts)
    assert set(granted) == {"D3", "WTCG"}
    assert presumed == frozenset()


def test_a_purely_licence_gated_catalog_presumes_nothing() -> None:
    facts = AccountFacts(licence_ids=frozenset({1}))
    granted, presumed = grant_ownership(_Catalog({"D3": _CATALOG["D3"]}), facts)
    assert set(granted) == {"D3"}
    assert presumed == frozenset()


def test_an_empty_catalog_presumes_nothing() -> None:
    granted, presumed = grant_ownership(_Catalog({}), AccountFacts(licence_ids=frozenset({1})))
    assert granted == {}
    assert presumed == frozenset()
