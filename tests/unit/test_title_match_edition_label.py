"""Tests for ``extract_edition_label``'s original-title slicing.

https://github.com/mubaraknumann/unifideck/pull/461#pullrequestreview-5307127830
item D.14: the previous implementation found the split point by
counting RAW words in the original title, but the boundary is computed
from the NORMALISED string's word count — an apostrophe adds a token
("Baldur's" -> "baldur s") and a hyphen can merge or drop one
("Half-Life" -> "half life"; a lone "-" -> zero tokens), so raw-word
counting misaligned the cut whenever a title had either. This exercises
the token-aligned fix (walk original words while tracking how many
normalised tokens each one contributes) against the exact titles the
review flagged.
"""
from __future__ import annotations

from unifideck.utils.title_match import extract_edition_label


def test_apostrophe_before_edition_suffix():
    assert (
        extract_edition_label("Baldur's Gate II: Enhanced Edition")
        == "Enhanced Edition"
    )


def test_hyphenated_compound_before_edition_suffix():
    assert extract_edition_label("Half-Life 2: Deluxe Edition") == "Deluxe Edition"


def test_trademark_and_hyphen_before_generic_suffix():
    assert (
        extract_edition_label("Bloodlines™ - Unofficial Patch")
        == "Unofficial Patch"
    )


def test_parenthesised_year_suffix_not_mangled():
    label = extract_edition_label("Star Wars: Battlefront (2005)")
    assert label == "(2005)"


def test_plain_title_with_no_suffix_returns_none():
    assert extract_edition_label("Half-Life 2") is None


def test_apostrophe_title_with_no_suffix_returns_none():
    assert extract_edition_label("Baldur's Gate II") is None
