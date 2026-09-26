"""Human-readable edition/variant label extraction for display.

Split out of the former monolithic ``title_match.py`` (2026-09-26,
file-size gate).
"""
from __future__ import annotations

import re

from .edition_suffixes import strip_edition_suffix, strip_edition_suffix_for_label_split
from .normalize import normalize_for_match


def _strip_wrapping_brackets(label: str) -> str:
    """Peel a balanced ``(...)``/``[...]`` pair that wraps all of
    ``label``, and drop a lone unmatched leading/trailing bracket the
    word-boundary slice in :func:`extract_edition_label` can leave
    behind.

    The slice cuts on whitespace, so a closing bracket glued to the
    last word survives a plain ``.strip()`` of bracket characters fine
    when its opener is also inside the label (``"(Xbox One)"`` →
    ``"Xbox One)"`` after the space-based cut is wrong either way,
    which is why this exists), but a bracket whose *partner* fell on
    the base-title side of the cut has no partner left to balance
    against — stripping just the character at the string's edge would
    leave the other one stranded in the middle (``"Standard Edition
    (Windows)"`` naively edge-stripped of ``)`` alone becomes
    ``"Standard Edition (Windows"``, an unmatched opener). Dropping the
    lone unmatched bracket instead of trying to re-pair it is the
    simpler correct behaviour — the label reads fine without it either
    way.
    """
    label = label.strip()
    if len(label) >= 2 and label[0] in "([" and label[-1] in ")]":
        return label[1:-1].strip()
    opens, closes = label.count("("), label.count(")")
    if opens != closes:
        label = label.replace("(", "") if opens > closes else label.replace(")", "")
    return label.strip()


# extract_edition_label's split point is found by counting NORMALISED
# tokens, not raw words, on both sides — counting raw words misaligns the
# cut whenever a word's normalised form doesn't map 1:1 (an apostrophe adds
# a token, "Baldur's" -> "baldur s"; a hyphenated compound merges two words
# into one token, "Half-Life" -> "half life"; a lone "-" normalises to zero
# tokens). Walking the original words while tracking how many normalised
# tokens each one contributes keeps the two token streams in lockstep, so
# the split lands on the right WORD, not a proportionally-wrong offset
# ("Baldur's Gate II: Enhanced Edition" used to slice at word 3 as if every
# word were one token, yielding just "Edition"; it now correctly yields
# "Enhanced Edition").
#
# A trailing release year is never included in the label, even as part of a
# longer discarded suffix — "Car Mechanic Simulator 2018" (bare year,
# nothing else) and "Microsoft Flight Simulator 2024 - Standard Edition"
# (year immediately followed by a real edition phrase) both keep their year
# attached to the base title rather than the label: annualised-franchise
# titles carry their year as part of the game's identity, not an edition.
# This is why the split point comes from strip_edition_suffix_for_label_split
# (never strips a trailing year) rather than strip_edition_suffix (matching
# stays permissive about years — see GROUPING_UNSAFE_SUFFIXES's docstring
# for why grouping is the one place a year is never negotiable) — UNLESS
# the year sits directly next to "edition" ("Sea of Thieves: 2025
# Edition"), which names a specific branded release rather than standing in
# for the base game's own version year.
def extract_edition_label(title: str) -> str | None:
    """Human-readable edition/variant suffix, case preserved.

    Companion to ``edition_suffixes.strip_edition_suffix``, which answers
    "what's the base title" by discarding the suffix entirely. This
    answers the opposite question — "what did we discard" — for display
    purposes (a duplicate-game card showing *which* store carries the
    Deluxe Edition). Slices the *original* (non-normalised) title at the
    equivalent word boundary so casing/punctuation reach the UI untouched
    (``"Cyberpunk 2077: Ultimate Edition"`` → ``"Ultimate Edition"``, not
    ``"ultimate edition"``) — see the comment above for exactly how the
    boundary and the year handling work.

    Returns ``None`` when the title carries no recognised edition suffix
    — most titles, including every sequel ("Beholder 2") since a bare
    version number is never in ``EDITION_SUFFIXES`` and doesn't match the
    generic ``<words> edition`` pattern either.
    """
    normalized = normalize_for_match(title)
    if not normalized:
        return None
    if strip_edition_suffix(normalized) == normalized:
        # No suffix at all, year or otherwise — nothing to label.
        return None
    if re.search(r"\b(?:19|20)\d{2}\s+edition\b", normalized):
        # "<year> Edition" is itself a self-contained branded suffix
        # ("Sea of Thieves: 2025 Edition") rather than an annualised
        # title's own version year with an unrelated edition phrase
        # tacked on after it ("Flight Simulator 2024 - Standard
        # Edition") — the year is adjacent to "edition" here, so it's
        # part of the thing being named, not the base game's identity.
        base = strip_edition_suffix(normalized)
    else:
        base = strip_edition_suffix_for_label_split(normalized)
    if base == normalized:
        return None
    base_token_count = len(base.split())

    words = title.split()
    consumed_tokens = 0
    for index, word in enumerate(words):
        if consumed_tokens >= base_token_count:
            label = " ".join(words[index:]).strip(" :-–—,")  # noqa: RUF001 — real en/em dashes appear in titles
            label = _strip_wrapping_brackets(label)
            # EDITION_SUFFIXES' "for pc"/"for windows"/"for xbox" entries
            # exist to strip the whole phrase for matching purposes, but
            # the leading preposition reads oddly as a label on its own
            # ("for Xbox") — the platform name alone is the useful part.
            label = re.sub(r"^for\s+", "", label, flags=re.IGNORECASE)
            return label or None
        word_normalized = normalize_for_match(word)
        consumed_tokens += len(word_normalized.split()) if word_normalized else 0
    return None
