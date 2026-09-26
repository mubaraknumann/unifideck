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

Every PUBLIC name below is re-exported here so existing callers
(``from unifideck.utils.title_match import X``) are unaffected by the
split — this module boundary is the stable public API, not the
submodule layout underneath it. Two callers outside this package also
reach one leading-underscore "private" name directly
(``core.game_grouping`` imports ``_strip_publisher_prefix`` and
``PUBLISHER_PREFIXES``) — a pre-existing convention from before the
split, kept working the same way via the explicit ``as``-self form
mypy requires to recognise a re-export. Every other underscore-prefixed
helper is genuinely internal to one submodule and stays unexported.
"""
from __future__ import annotations

from .edition_label import extract_edition_label as extract_edition_label
from .edition_suffixes import EDITION_SUFFIXES as EDITION_SUFFIXES
from .edition_suffixes import GROUPING_UNSAFE_SUFFIXES as GROUPING_UNSAFE_SUFFIXES
from .edition_suffixes import strip_edition_suffix as strip_edition_suffix
from .edition_suffixes import (
    strip_edition_suffix_for_grouping as strip_edition_suffix_for_grouping,
)
from .matching import PUBLISHER_PREFIXES as PUBLISHER_PREFIXES
from .matching import _strip_publisher_prefix as _strip_publisher_prefix
from .matching import clean_search_query as clean_search_query
from .matching import leftover_word_count as leftover_word_count
from .matching import score_match as score_match
from .matching import titles_match as titles_match
from .normalize import normalize_for_match as normalize_for_match
from .normalize import version_tokens as version_tokens

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
