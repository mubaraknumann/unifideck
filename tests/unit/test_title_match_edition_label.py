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


def test_parenthesised_year_suffix_is_not_an_edition():
    """A bare release year, parenthesised or not, is part of the game's
    identity ("Battlefront (2005)" vs "...(2017)" name different
    products) — not an edition/variant, so it must not be surfaced as
    one. Found via a live-library audit: this used to return the
    mangled "(2005)"; the fix is to not treat it as a label at all."""
    label = extract_edition_label("Star Wars: Battlefront (2005)")
    assert label is None


def test_plain_title_with_no_suffix_returns_none():
    assert extract_edition_label("Half-Life 2") is None


def test_apostrophe_title_with_no_suffix_returns_none():
    assert extract_edition_label("Baldur's Gate II") is None


# Found via a live-library audit (2583 real titles), covering three
# distinct bug classes beyond the D.14 token-alignment fix above:
# bare years read as an edition, a comma/parenthesis breaking the same
# token-alignment the apostrophe/hyphen cases above already fixed, and
# common title-ending words too eagerly listed as edition suffixes.


def test_bare_trailing_year_is_not_an_edition():
    """Annualised-franchise titles carry their year as part of the
    game's identity, not an edition/variant — "Car Mechanic Simulator
    2018" is a complete, correct title on its own."""
    assert extract_edition_label("Car Mechanic Simulator 2018") is None
    assert extract_edition_label("Football Manager 2023") is None


def test_year_alongside_a_real_suffix_still_shown():
    """The bare-year guard must not swallow a year that's part of a
    genuinely branded, multi-word edition name rather than standing
    alone — only a *bare* trailing year is suppressed."""
    assert (
        extract_edition_label("Sea of Thieves: 2025 Edition") == "2025 Edition"
    )


def test_comma_inside_parenthetical_does_not_misalign_the_cut():
    """A comma-joined parenthetical ("(Classic, 2004)") used to misalign
    the raw-word walk the same way apostrophes/hyphens did (D.14) —
    "(Classic," normalises to one token ("classic") from one raw word,
    which is fine, but the walk previously stopped one word early and
    returned a mangled leftover fragment. The parenthetical remainder
    is itself not a recognised edition (it survives from the trailing-
    year guard, not a real suffix table entry), so the whole thing
    correctly returns None rather than a partial, bracket-mismatched
    string."""
    label = extract_edition_label("STAR WARS™ Battlefront (Classic, 2004)")
    assert label is None


def test_parens_are_stripped_from_a_real_label():
    """A genuine edition/platform suffix that happens to be
    parenthesised in the original title must not leave a stray bracket
    in the displayed label."""
    assert extract_edition_label("Grand Theft Auto V (Xbox One)") == "Xbox One"


def test_revolution_as_a_title_word_is_not_an_edition():
    """Bare "revolution" was in EDITION_SUFFIXES as a standalone
    suffix, but it's also the last word of real, unrelated titles —
    "Cyber Revolution" and "Duel Revolution" are complete titles, not
    editions of "Cyber"/"Duel"."""
    assert extract_edition_label("Cyber Revolution") is None
    assert extract_edition_label("Duel Revolution") is None


def test_console_as_a_title_word_is_not_an_edition():
    """Same false-positive class as "revolution" above — "Creative
    Console" is a complete title, not a console-platform edition of
    "Creative". The multi-word "console edition" entry is unaffected
    and still strips correctly (see below)."""
    assert extract_edition_label("Creative Console") is None


def test_console_edition_phrase_still_strips():
    assert extract_edition_label("Stellaris: Console Edition") == "Console Edition"


def test_annualised_years_year_stays_out_of_a_real_edition_label():
    """A trailing release year that ISN'T adjacent to "edition" belongs
    to the base title, not the label, even when a real edition phrase
    follows it later in the title — "2024" here is Flight Simulator's
    own version year, not part of "Standard Edition"."""
    label = extract_edition_label(
        "Microsoft Flight Simulator 2024 - Standard Edition",
    )
    assert label == "Standard Edition"


def test_year_immediately_before_edition_is_a_real_branded_label():
    """The opposite case: a year directly adjacent to "Edition" names a
    specific yearly release of the edition itself (Sea of Thieves does
    this) rather than being the base game's own version marker, so it
    stays part of the label."""
    assert extract_edition_label("Sea of Thieves: 2025 Edition") == "2025 Edition"


def test_for_platform_suffix_drops_the_preposition():
    """EDITION_SUFFIXES' "for pc"/"for windows"/"for xbox" entries strip
    the whole phrase for matching, but "for Xbox" alone reads oddly as a
    label — the platform name is the useful part."""
    assert extract_edition_label("RESIDENT EVIL 3 for Xbox") == "Xbox"


def test_unmatched_closing_bracket_does_not_strand_the_opener():
    """The label's opening word-boundary cut can land AFTER an opening
    paren whose text (the base title) precedes it — "Standard Edition
    (Windows)" splits into base "...II -" / label "Standard Edition
    (Windows)", where the label's own '(' is matched by its own ')' and
    both must survive; a naive edge-only bracket strip would strip only
    the trailing ')' and strand the '(' in the middle."""
    label = extract_edition_label(
        "Call of Duty®: Modern Warfare® II - Standard Edition (Windows)",
    )
    assert label == "Standard Edition (Windows)"
    assert label.count("(") == label.count(")")
