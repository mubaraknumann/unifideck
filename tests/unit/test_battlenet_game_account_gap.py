"""The free-to-play gap is measured, not silent.

Audit §3.5, finding A. ``BattlenetStore._cached_game_accounts`` reads a
``game_accounts`` cache key that **nothing in the tree ever writes** — the
consumer shipped with the initial Battle.net integration and the producer
was never built. So ``AccountFacts.game_account_programs`` is always empty,
``rules._match_game_account`` can never match, and every title whose catalog
rule keys on ``game_account`` rather than ``license_id`` is dropped: the
free-to-play and subscription set. ``library.py``'s own header measures it
on a real account — 17 programs from licences, 22 with game accounts.

Two things kept that hidden, and both are pinned here:

* the consumer's docstring described the producer as if it existed;
* ``test_battlenet_ownership`` proves the rule engine handles game accounts
  by hand-building the facts, so it passes while production is empty.

These tests assert the *gap is visible* rather than closed. They should
keep passing when the producer lands: ``count_game_account_gated`` returns
0 once facts exist, which is the honest signal either way.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from unifideck.stores.battlenet.library import count_game_account_gated
from unifideck.stores.battlenet.ownership import AccountFacts

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


def test_gap_is_counted_when_game_account_facts_are_missing() -> None:
    """The free title is missing and the count says so."""
    facts = AccountFacts(licence_ids=frozenset({1}))
    assert count_game_account_gated(_Catalog(_CATALOG), facts) == 1


def test_no_gap_reported_once_the_facts_exist() -> None:
    """The producer landing must silence this, not keep warning."""
    facts = AccountFacts(
        licence_ids=frozenset({1}),
        game_account_programs=frozenset({"WTCG"}),
    )
    assert count_game_account_gated(_Catalog(_CATALOG), facts) == 0


def test_no_gap_reported_for_a_purely_licence_gated_catalog() -> None:
    """No false alarm when the account really does own everything."""
    facts = AccountFacts(licence_ids=frozenset({1}))
    only_licences = {"D3": _CATALOG["D3"]}
    assert count_game_account_gated(_Catalog(only_licences), facts) == 0


def test_empty_catalog_reports_no_gap() -> None:
    facts = AccountFacts(licence_ids=frozenset({1}))
    assert count_game_account_gated(_Catalog({}), facts) == 0


def test_the_cache_key_the_store_reads_still_has_no_writer() -> None:
    """A failing-by-design marker for the producer's own change.

    This is the whole of finding A in one assertion: the read exists, the
    write does not. When the producer lands, this test is what tells you
    to delete it — and if it ever passes again after that, the producer
    has regressed to silence.
    """
    root = Path(__file__).resolve().parents[2]
    writers = []
    for path in (root / "py_modules").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if "game_accounts" not in line:
                continue
            # A write goes through CacheManager.set for the battlenet
            # namespace; the known read uses .get and logout uses .clear.
            if ".set(" in line:
                writers.append(f"{path.name}: {line.strip()}")
    assert writers == [], (
        "a writer for the battlenet game_accounts cache now exists — "
        "finding A is closed, so delete this test and the "
        "count_game_account_gated warning it guards"
    )
