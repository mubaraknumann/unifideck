"""Store-agnostic title-matching primitives.

Pure functions + the edition-suffix table. Pure means no I/O,
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
* :mod:`.edition_suffixes` — the suffix table, edition and platform
  stripping.
* :mod:`.edition_label` — display-oriented "what edition is this"
  extraction, built on the above.
* :mod:`.matching` — fuzzy ``titles_match``/scoring, publisher
  prefixes, search-query cleanup.

Every PUBLIC name below is re-exported here so existing callers
(``from unifideck.utils.title_match import X``) are unaffected by the
split — this module boundary is the stable public API, not the
submodule layout underneath it. ``core.game_identity`` also reaches one
leading-underscore name, ``_strip_publisher_prefix``, through the explicit
``as``-self form mypy requires to recognise a re-export. Two more internal
helpers are re-exported under public names for it:
``fold_roman_numerals`` and ``strip_celebration_suffix``. Every other
underscore-prefixed helper is internal to one submodule.
"""
from __future__ import annotations

from .edition_label import extract_edition_label as extract_edition_label
from .edition_suffixes import EDITION_SUFFIXES as EDITION_SUFFIXES
from .edition_suffixes import PLATFORM_SUFFIXES as PLATFORM_SUFFIXES
from .edition_suffixes import TITLE_WORD_SUFFIXES as TITLE_WORD_SUFFIXES
from .edition_suffixes import _strip_celebration as strip_celebration_suffix
from .edition_suffixes import strip_edition_suffix as strip_edition_suffix
from .edition_suffixes import strip_platform_suffix as strip_platform_suffix
from .matching import PUBLISHER_PREFIXES as PUBLISHER_PREFIXES
from .matching import _strip_publisher_prefix as _strip_publisher_prefix
from .matching import clean_search_query as clean_search_query
from .matching import score_match as score_match
from .matching import titles_match as titles_match
from .normalize import _fold_roman_numerals as fold_roman_numerals
from .normalize import normalize_for_match as normalize_for_match
from .normalize import version_tokens as version_tokens

__all__ = [
    "EDITION_SUFFIXES",
    "PLATFORM_SUFFIXES",
    "PUBLISHER_PREFIXES",
    "TITLE_WORD_SUFFIXES",
    "clean_search_query",
    "extract_edition_label",
    "fold_roman_numerals",
    "normalize_for_match",
    "score_match",
    "strip_celebration_suffix",
    "strip_edition_suffix",
    "strip_platform_suffix",
    "titles_match",
    "version_tokens",
]
