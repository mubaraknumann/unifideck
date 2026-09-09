"""The SteamGridDB ladder must never return a sibling sequel's art.

Regression test for the duplicate-artwork report: a tester's seven
Ubisoft Settlers games all showed one cover. The edition-phrase strip
collapsed "The Settlers N - History Edition" to the bare franchise on
both sides, and pass 6's 0.50 fuzzy floor then accepted a sibling
(a cross-sequel pair scores ~0.67, comfortably above it).

The candidate rows below are the real SteamGridDB autocomplete payload
for these queries, trimmed to the fields the ladder reads.
"""
from __future__ import annotations

import pytest

from unifideck.steam.steamgriddb import search as sgdb_search

# Real SGDB autocomplete rows (id + name), as returned for "The Settlers …".
_SETTLERS_RESULTS = [
    {"id": 5304853, "name": "The Settlers: History Edition"},
    {"id": 32344, "name": "The Settlers 7 : History Edition"},
    {"id": 5271721, "name": "The Settlers 3: History Edition"},
    {"id": 5304852, "name": "The Settlers IV: History Edition"},
    {"id": 32342, "name": "The Settlers : Heritage of Kings - History Edition"},
    {"id": 1472, "name": "The Settlers: Heritage of Kings"},
]


@pytest.fixture
def stub_autocomplete(monkeypatch):
    """Serve ``_SETTLERS_RESULTS`` for every autocomplete call."""
    calls: list[str] = []

    async def _fake(session, base, api_key, query, timeout_sec):
        calls.append(query)
        return list(_SETTLERS_RESULTS)

    monkeypatch.setattr(sgdb_search, "_autocomplete", _fake)
    return calls


async def _resolve(title: str) -> int | None:
    return await sgdb_search.search_game_id(
        None, "https://sgdb.test/api/v2", "key", title, timeout_sec=5,
    )


@pytest.mark.parametrize(("title", "expected"), [
    # The number picks its own entry out of a field of siblings.
    ("The Settlers 7 - History Edition", 32344),
    ("The Settlers 3 - History Edition", 5271721),
    # Roman numeral on the candidate side still folds to the query's "4".
    ("The Settlers 4 - History Edition", 5304852),
    # No number matches only the no-number entry.
    ("The Settlers - History Edition", 5304853),
])
async def test_resolves_to_its_own_entry(stub_autocomplete, title, expected):
    assert await _resolve(title) == expected


@pytest.mark.parametrize("title", [
    "The Settlers 2 - History Edition",
    "The Settlers 5 - History Edition",
    "The Settlers 6 - History Edition",
])
async def test_absent_sequel_returns_none_not_a_sibling(
    stub_autocomplete, title,
):
    """SGDB holds no entry for these, so the answer is None.

    Before the version guard each of them returned 32344 (Settlers 7),
    which is how one cover ended up on four different games. Falling
    through to the Steam CDN is the correct outcome: callers prefer no
    art over another game's art.
    """
    assert await _resolve(title) is None
