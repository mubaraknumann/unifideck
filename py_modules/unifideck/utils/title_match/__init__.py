"""Store-agnostic title-matching primitives.

Pure functions + the 58-entry edition-suffix table. Pure means no I/O,
no async, no logging — testable in isolation. Originally written for the
SGDB 6-pass ``search_game_id`` ladder, but normalisation / edition
stripping / Jaccard scoring are storefront-independent, so this is the
shared home: any feature that resolves a free-form game title to an
external id (SGDB artwork, Steam ``storesearch``, metadata, compat)
should clean titles through these helpers so matching stays consistent.

Why these matter
================
A storefront's search returns the *first* alphabetical-ish result that
matches the substring. Without normalisation and edition stripping:

* ``Watch Dogs®2 - Deluxe Edition`` → autocomplete returns nothing
  (the ® character breaks the match).
* ``Assassin's Creed`` → autocomplete returns ``Assassin's Creed
  Odyssey`` first (substring match wins; the user wanted the
  original game).
* ``EA SPORTS FC 25`` → autocomplete misses ``FC 25`` because the
  SGDB entry is indexed without the publisher prefix.

Each pass uses these helpers to widen the matching net without ever
accepting a wrong game (the 0.85 Jaccard threshold prevents franchise
confusion).

The one thing widening must never dissolve is the *version* — see
:func:`version_tokens`. A sequel number is a single word, and the
edition stripper used to eat it: "The Settlers N - History Edition"
collapsed to "the settlers" for every N, so one SteamGridDB entry
supplied the artwork for seven different games. An *episode* number is
not a version (see ``edition_suffixes._strip_chapters_episodes``) —
episodes of one season share their season's art on purpose.

Package layout (split 2026-09-26, file-size gate — was one 736-line
module; see git history for the pre-split version):

* :mod:`.normalize` — string normalisation, sequel/version tokens.
* :mod:`.edition_suffixes` — the suffix table + matching-oriented and
  grouping-oriented stripping.
* :mod:`.edition_label` — display-oriented "what edition is this"
  extraction, built on the above.
* :mod:`.matching` — fuzzy ``titles_match``/scoring, publisher
  prefixes, search-query cleanup.

Every name below is re-exported here so existing callers
(``from unifideck.utils.title_match import X``) are unaffected by the
split — this module boundary is the stable public API, not the
submodule layout underneath it.
"""
from __future__ import annotations

from .edition_label import (
    _strip_wrapping_brackets,
    extract_edition_label,
)
from .edition_suffixes import (
    EDITION_SUFFIXES,
    GROUPING_UNSAFE_SUFFIXES,
    _strip_celebration,
    _strip_chapters_episodes,
    _strip_edition_phrase,
    _strip_known_suffix,
    _strip_known_suffix_for_grouping,
    _strip_trailing_year,
    strip_edition_suffix,
    strip_edition_suffix_for_grouping,
)
from .edition_suffixes import (
    strip_edition_suffix_for_label_split as _strip_edition_suffix_for_label_split,
)
from .matching import (
    _EDITION_TOKENS,
    PUBLISHER_PREFIXES,
    _core_title_match,
    _is_edition_remainder,
    _strip_publisher_prefix,
    clean_search_query,
    leftover_word_count,
    score_match,
    titles_match,
)
from .normalize import (
    _ROMAN_TO_ARABIC,
    _fold_roman_numerals,
    normalize_for_match,
    version_tokens,
)

__all__ = [
    "EDITION_SUFFIXES",
    "GROUPING_UNSAFE_SUFFIXES",
    "PUBLISHER_PREFIXES",
    "clean_search_query",
    "extract_edition_label",
    "leftover_word_count",
    "normalize_for_match",
    "score_match",
    "strip_edition_suffix",
    "strip_edition_suffix_for_grouping",
    "titles_match",
    "version_tokens",
]
