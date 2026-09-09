"""Generic title matcher used by the Steam resolvers (artwork/metadata/compat).

Blind ``items[0]`` from Steam ``storesearch`` produced wrong metadata +
compat across the library — sequels (``Hades`` → ``Hades II``), soundtracks
(``Figment`` → ``Figment - Soundtrack``), and unrelated hits (``Control`` →
``Steam Controller``). ``titles_match`` rejects those while still accepting
®/™/apostrophe, edition/year, publisher-prefix and Roman-numeral variants.
Validated against the real 1172-game library (94 wrong mappings flagged).
"""
from __future__ import annotations

import pytest

from unifideck.utils.title_match import titles_match

# (query, candidate, expected)
_ACCEPT = [
    ("Control", "CONTROL Ultimate Edition"),                       # edition
    ("Sea of Thieves", "Sea of Thieves: 2026 Edition"),            # year edition
    ("Tom Clancy's Splinter Cell", "Tom Clancy's Splinter Cell®"),  # ®
    ("Splinter Cell Chaos Theory", "Tom Clancy's Splinter Cell Chaos Theory®"),  # publisher prefix
    ("Sid Meier's Civilization VI", "Civilization VI"),            # publisher prefix
    ("Assassin's Creed II", "Assassin's Creed 2"),                 # roman↔arabic
    ("Thief™ 2: The Metal Age", "Thief II: The Metal Age"),        # arabic↔roman
    ("No Man's Sky", "No Man's Sky"),                              # apostrophe exact
    ("DOOM Eternal", "DOOM Eternal"),                              # exact
    ("Rise of the Tomb Raider: 20 Year Celebration",
     "Rise of the Tomb Raider™"),                                  # N-Year Celebration re-release tag
    ("Tomb Raider: Anniversary Celebration", "Tomb Raider"),       # anniversary celebration
    # A sequel number survives an edition suffix, so the RIGHT sibling
    # still matches (Roman on either side included).
    ("The Settlers 7 - History Edition", "The Settlers 7 : History Edition"),
    ("The Settlers 4 - History Edition", "The Settlers IV: History Edition"),
    ("The Settlers - History Edition", "The Settlers: History Edition"),
]

_REJECT = [
    ("Hades", "Hades II"),                  # sequel
    ("Quake", "Quake II"),                  # sequel (roman)
    ("Ghostrunner", "Ghostrunner 2"),       # sequel
    ("Frostpunk", "Frostpunk 2"),           # sequel
    ("The Outer Worlds", "The Outer Worlds 2"),
    ("Figment", "Figment - Soundtrack"),    # soundtrack
    ("Control", "Steam Controller"),        # unrelated
    ("Calico", "Quilts and Cats of Calico"),  # unrelated
    ("Halo", "Halo 5: Guardians"),          # bare franchise vs entry
    # Sequel number destroyed by the edition-phrase strip: every
    # "<game> N - <word> Edition" collapsed to the bare franchise, so
    # the whole Settlers set resolved to one SteamGridDB entry and the
    # tester saw a single cover on seven games. Same shape in DOOM /
    # Darksiders / Total War / Anno.
    ("The Settlers 5 - History Edition", "The Settlers 7 : History Edition"),
    ("The Settlers 2 - History Edition", "The Settlers 3: History Edition"),
    ("The Settlers", "The Settlers 7 : History Edition"),
    ("Anno 1404 - History Edition", "Anno 1701 - History Edition"),
    ("DOOM 3: BFG Edition", "DOOM"),
    ("Darksiders II Deathinitive Edition", "Darksiders"),
    ("Total War: Rome II - Emperor Edition", "Total War: Rome"),
]


@pytest.mark.parametrize(("q", "c"), _ACCEPT)
def test_titles_match_accepts_variants(q, c):
    assert titles_match(q, c) is True


@pytest.mark.parametrize(("q", "c"), _REJECT)
def test_titles_match_rejects_wrong(q, c):
    assert titles_match(q, c) is False


def test_titles_match_empty_inputs():
    assert titles_match("", "Anything") is False
    assert titles_match("Anything", "") is False
