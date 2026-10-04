"""Platform words in a title must not hide the game from Steam's search.

Xbox product titles carry platform markers that Steam's names never have:
"DOOM Eternal Standard Edition (PC)", "Mafia: Definitive Edition for XBOX
One". Steam's storesearch returns nothing for those strings (measured
2026-10-02), so the owned game never showed OWNED on its Steam page.

Pinned here:

- platform words are stripped, and nothing else, for the narrow retry;
- the narrow retry runs before the edition-stripping one, so an edition
  that is its own Steam game is found before its bare franchise;
- a title with no platform or edition words costs no extra request.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.steam import library
from unifideck.utils.title_match import (
    normalize_for_match,
    strip_edition_suffix,
    strip_platform_suffix,
    titles_match,
)


@pytest.mark.parametrize(("title", "expected"), [
    ("DOOM Eternal Standard Edition (PC)", "doom eternal standard edition"),
    ("Wolfenstein: The Old Blood (PC)", "wolfenstein the old blood"),
    ("Mafia: Definitive Edition for XBOX One", "mafia definitive edition"),
    ("Halo 5 for Xbox Series X|S", "halo 5"),
    ("Control Ultimate Edition", "control ultimate edition"),
    ("PC Building Simulator", "pc building simulator"),  # only trailing words go
])
def test_only_platform_words_are_stripped(title: str, expected: str) -> None:
    assert strip_platform_suffix(normalize_for_match(title)) == expected


@pytest.mark.parametrize(("title", "expected"), [
    ("DOOM Eternal Standard Edition (PC)", "doom eternal"),
    ("Call of Duty®: Modern Warfare® - Digital Standard Edition",
     "call of duty modern warfare"),
])
def test_the_full_strip_reaches_the_base_game(title: str, expected: str) -> None:
    assert strip_edition_suffix(normalize_for_match(title)) == expected


def test_the_steam_name_still_has_to_match_the_full_title() -> None:
    assert titles_match("DOOM Eternal Standard Edition (PC)", "DOOM Eternal")
    assert titles_match("Wolfenstein: The Old Blood (PC)", "Wolfenstein: The Old Blood")
    # A separate product, not the game: "battlemode" is not edition noise.
    assert not titles_match("DOOM Eternal (BATTLEMODE - PC)", "DOOM Eternal")


def _fake_search(
    monkeypatch: pytest.MonkeyPatch, results: dict[str, list[dict[str, Any]]],
) -> list[str]:
    queries: list[str] = []

    async def _items(query: str, timeout_s: float, session: Any) -> list[dict[str, Any]]:
        queries.append(query)
        return results.get(query, [])

    monkeypatch.setattr(library, "_storesearch_items", _items)
    return queries


async def test_doom_reaches_its_steam_page(monkeypatch: pytest.MonkeyPatch) -> None:
    queries = _fake_search(monkeypatch, {"doom eternal": [{"id": 782330, "name": "DOOM Eternal"}]})

    result = await library.search_store("DOOM Eternal Standard Edition (PC)")

    assert result is not None and result["app_id"] == 782330
    assert queries == [
        "DOOM Eternal Standard Edition (PC)",
        "doom eternal standard edition",
        "doom eternal",
    ]


async def test_an_edition_is_searched_before_its_franchise(monkeypatch: pytest.MonkeyPatch) -> None:
    """"mafia" alone finds the 2002 game first, and it would pass the
    title match ("Definitive Edition" is edition noise to the matcher)."""
    queries = _fake_search(monkeypatch, {
        "mafia definitive edition": [{"id": 1030840, "name": "Mafia: Definitive Edition"}],
        "mafia": [{"id": 40990, "name": "Mafia"}],
    })

    result = await library.search_store("Mafia: Definitive Edition for XBOX One")

    assert result is not None and result["app_id"] == 1030840
    assert "mafia" not in queries


async def test_a_plain_title_costs_one_search(monkeypatch: pytest.MonkeyPatch) -> None:
    queries = _fake_search(monkeypatch, {})

    assert await library.search_store("Hollow Knight") is None
    assert queries == ["Hollow Knight"]
