"""Sequel discriminators — the one part of a title no strip may eat.

``version_tokens`` is what stops "The Settlers 5 - History Edition" from
being treated as "The Settlers 7 - History Edition". The edition-phrase
stripper consults it before consuming words, and the SteamGridDB ladder
consults it before scoring a candidate, so its edge cases (years vs
sequel numbers, Roman numerals, ordinals) decide real artwork matches.
"""
from __future__ import annotations

import pytest

from unifideck.utils.title_match import (
    normalize_for_match,
    strip_edition_suffix,
    titles_match,
    version_tokens,
)


@pytest.mark.parametrize(("normalized", "expected"), [
    # Bare sequel numbers.
    ("doom 3", {"3"}),
    ("the settlers 7 history edition", {"7"}),
    # Roman numerals fold to Arabic so "II" and "2" agree.
    ("the settlers ii", {"2"}),
    ("civilization vi", {"6"}),
    # Single-letter Roman numerals stay excluded — too often branding.
    ("mega man x", set()),
    # 4-digit years 1980-2030 are an edition tag, not a sequel number.
    ("sea of thieves 2026 edition", set()),
    ("call of duty 1999", set()),
    # ...but Anno's numbers fall outside that window and DO discriminate.
    ("anno 1404", {"1404"}),
    ("anno 1701", {"1701"}),
    ("anno 1602", {"1602"}),
    # Ordinals are words, not version markers.
    ("the settlers 2 10th anniversary", {"2"}),
    # No marker at all is itself a value.
    ("control", set()),
    ("", set()),
])
def test_version_tokens(normalized, expected):
    assert version_tokens(normalized) == expected


@pytest.mark.parametrize("title", [
    "The Settlers 7 - History Edition",
    "DOOM 3: BFG Edition",
    "Anno 1404 - History Edition",
    "Darksiders II Deathinitive Edition",
    "Total War: Rome II - Emperor Edition",
])
def test_strip_never_eats_the_version(title):
    """The number must survive edition stripping."""
    normalized = normalize_for_match(title)
    assert version_tokens(strip_edition_suffix(normalized)) == version_tokens(
        normalized,
    )


@pytest.mark.parametrize(("episode", "season"), [
    ("The Walking Dead: A New Frontier - Episode 1",
     "The Walking Dead: A New Frontier"),
    ("Tales of Monkey Island: Chapter 3", "Tales of Monkey Island"),
])
def test_episodes_still_collapse_to_their_season(episode, season):
    """An episode number is NOT a sequel number.

    Episodes of one season are parts of a single game and storefronts
    carry one artwork entry for the season, so they must keep matching
    it — otherwise an episodic shortcut ends up with no art at all.
    """
    assert titles_match(episode, season) is True


@pytest.mark.parametrize(("title", "expected_base"), [
    # Guarding the version must not stop legitimate edition stripping.
    ("CONTROL Ultimate Edition", "control"),
    ("The Settlers® 2: Gold Edition", "the settlers 2"),
    # The edition-phrase strip used to run a non-greedy 2-word window,
    # so it removed as MUCH as it could and swallowed ordinary title
    # words along with the tag: these based to "sea of", "halo the
    # master" and "the settlers heritage of".
    ("Sea of Thieves: 2026 Edition", "sea of thieves"),
    ("Halo: The Master Chief Collection Edition",
     "halo the master chief collection"),
    ("The Settlers: Heritage of Kings - History Edition",
     "the settlers heritage of kings history"),
])
def test_edition_stripping_still_works(title, expected_base):
    assert strip_edition_suffix(normalize_for_match(title)) == expected_base


def test_year_edition_is_not_a_version_so_it_still_matches():
    """A 4-digit year is an edition tag, not a sequel number."""
    assert version_tokens("sea of thieves 2026 edition") == set()
    assert titles_match("Sea of Thieves", "Sea of Thieves: 2026 Edition")


@pytest.mark.parametrize(("base_game", "edition"), [
    # Arbitrarily-named editions the 58-entry table does not list. The
    # old non-greedy window absorbed these by over-stripping; they are
    # now recognised by _is_edition_remainder instead, with no risk to
    # the title words.
    ("For Honor", "For Honor - Marching Fire Edition"),
    ("Overcooked", "Overcooked: Gourmet Edition"),
    ("The Outer Worlds", "The Outer Worlds: Spacer's Choice Edition"),
])
def test_named_editions_still_match_their_base_game(base_game, edition):
    assert titles_match(base_game, edition) is True


@pytest.mark.parametrize(("query", "candidate"), [
    # ...and that permissiveness must not bridge two different sequels.
    ("The Settlers", "The Settlers 7 : History Edition"),
    ("Anno 1404", "Anno 1701 - History Edition"),
    ("Darksiders", "Darksiders II Deathinitive Edition"),
])
def test_named_edition_rule_cannot_bridge_sequels(query, candidate):
    assert titles_match(query, candidate) is False
