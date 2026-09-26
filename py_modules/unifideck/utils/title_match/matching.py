"""Fuzzy title-matching, scoring, and search-query cleanup.

Split out of the former monolithic ``title_match.py`` (2026-09-26, file-size
gate). This is the top-level "is this storefront result the game I searched
for" API (:func:`titles_match`) plus the scoring/publisher-prefix machinery
it's built from.
"""
from __future__ import annotations

import re

from .edition_suffixes import EDITION_SUFFIXES, strip_edition_suffix
from .normalize import _fold_roman_numerals, normalize_for_match, version_tokens


def score_match(query_norm: str, candidate_norm: str) -> float:
    """Jaccard word-set overlap with prefix-match bonus.

    Returns a value in ``[0.0, 1.0]``. Caller compares against a
    threshold (0.85 for strict confidence, 0.50 for fuzzy fallback).

    Strict bounds:

    * identical strings → 1.0
    * same words, different order → 0.95
    * franchise-confusion guard: ``"assassins creed"`` vs
      ``"assassins creed odyssey"`` → 0.67 (rejected at 0.85)
    * prefix bonus: when all query words appear at the start of the
      candidate (handles truncated shortcut names like ``"Kameo"``
      finding ``"Kameo: Elements of Power"``).
    """
    if not query_norm or not candidate_norm:
        return 0.0
    if query_norm == candidate_norm:
        return 1.0
    qw = set(query_norm.split())
    cw = set(candidate_norm.split())
    if qw == cw:
        return 0.95
    intersection = qw & cw
    union = qw | cw
    jaccard = len(intersection) / len(union) if union else 0.0

    ql = query_norm.split()
    cl = candidate_norm.split()
    if len(ql) <= len(cl) and ql == cl[: len(ql)]:
        prefix_score = max(0.50, len(ql) / len(cl))
        jaccard = max(jaccard, prefix_score)
    return jaccard


def leftover_word_count(query_norm: str, candidate_norm: str) -> int:
    """Words in ``candidate_norm`` that aren't also in ``query_norm``.

    Tie-breaker for "which of several ``titles_match``-accepted
    candidates is the best one" (see ``core.game_grouping``'s
    Steam-owned resolver): given several real Steam titles that all
    fuzzy-match a query, the one with the fewest extra words is the
    closest fit. ``"Thief"`` vs candidates ``"Thief Gold"`` (1 leftover:
    "gold") and ``"Thief"`` (0 leftover) picks the exact ``"Thief"``
    over ``"Thief Gold"`` — the bug this exists to fix was the reverse:
    dict-iteration order deciding which of several title-matching Steam
    appids won, independent of fit quality.
    """
    qw = set(query_norm.split())
    cw = set(candidate_norm.split())
    return len(cw - qw)


def clean_search_query(title: str) -> str:
    """Pre-API query cleanup — strip noise that hurts SGDB autocomplete.

    Different from :func:`~unifideck.utils.title_match.normalize_for_match`
    — that one prepares a string for *comparison*; this one prepares a
    string for *sending to the API*. SGDB autocomplete is forgiving but
    does worse on titles with platform/edition suffixes attached because
    it returns the first substring match and edition-tagged entries sort
    later.

    Strips:

    * ® ™ ©
    * trailing ``- CE``/``- SE``/``- DE``/``- GE`` markers
    * parenthesised platform tags ``(Xbox One)``, ``(PC)`` etc.
    * trailing ``- Xbox One Edition`` / ``for Xbox`` / etc.
    * ``- Cross Gen Bundle`` / ``- The Complete Season``
    * ``- Standard Edition`` / ``- Console Edition``
    * parenthesised years ``(2020)``
    * parenthesised ``(X|S)``
    * parenthesised ``(Episodes 1-5)``
    * ``+ Something DLC`` add-ons
    * trailing ``Xbox One Version`` / ``Digital Version``
    """
    q = re.sub(r"[®™©]", "", title).strip()
    # En-dash (–) in these regexes is intentional — titles like
    # "Forza Horizon – Standard Edition" need to match both hyphen
    # and en-dash separators. RUF001 flags it as ambiguous; we know.
    patterns = (
        r"\s*[-–:]\s*(?:CE|SE|DE|GE)\s*$",  # noqa: RUF001
        (r"\s*\((?:Xbox (?:One|Series X\|?S)|PC|Windows|PS[45]|"
         r"Nintendo Switch|Game Preview)\)\s*$"),
        r"\s*[-–:]\s*Xbox (?:One|Series X\|?S)(?:\s+Edition)?\s*$",  # noqa: RUF001
        r"\s+for\s+Xbox\s*$",
        r"\s+Xbox\s+(?:One|Series\s+X\|?S)(?:\s+Edition)?\s*$",
        (r"\s*[-–:]\s*(?:Cross[- ]Gen\s+(?:Bundle|Edition)|"  # noqa: RUF001
         r"The\s+Complete(?:\s+First)?\s+Season)\s*$"),
        (r"\s*[-–:]\s*(?:Standard|Console)\s+Edition"  # noqa: RUF001
         r"(?:\s*\(Windows\))?\s*$"),
        r"\s*\(\d{4}\)",
        r"\s*\(X\|?S\)",
        r"\s*\((?:Episodes?|Chapters?)\s+[\d\-\s]+\)",
        r"\s*\+\s+.+$",
        r"\s+Xbox\s+One\s+Version\s*$",
    )
    for pat in patterns:
        q = re.sub(pat, "", q, flags=re.IGNORECASE).strip()
    return q


# Publisher prefixes in *normalised* form — ``normalize_for_match`` turns
# "Tom Clancy's" into ``tom clancy s`` (the apostrophe becomes a space), so
# the prefix entries MUST carry that trailing ``s`` token or they never
# match. A storefront often indexes a game without its publisher branding
# ("Splinter Cell Chaos Theory" vs Steam's "Tom Clancy's Splinter Cell
# Chaos Theory"); stripping a known prefix from either side recovers the
# match without risking a false positive (the remainder still has to match).
PUBLISHER_PREFIXES: tuple[str, ...] = (
    "ea sports", "tom clancy s", "sid meier s", "disney pixar",
    "dreamworks", "marvel s", "warner bros", "2k", "microsoft", "disney",
)

# Tokens that, as the *only* leftover after a common prefix, mark the longer
# title as an edition/year variant of the shorter (so "Sea of Thieves" still
# matches Steam's "Sea of Thieves: 2026 Edition"). Built from the edition
# table plus filler words; a bare 4-digit year also qualifies.
_EDITION_TOKENS: frozenset[str] = frozenset(
    {"edition", "of", "the", "year", "game"}
    | {word for suffix in EDITION_SUFFIXES for word in suffix.split()},
)


def _strip_publisher_prefix(normalized: str) -> str:
    """Drop one leading known publisher prefix from a normalised title."""
    for prefix in PUBLISHER_PREFIXES:
        if normalized.startswith(prefix + " "):
            return normalized[len(prefix):].strip()
    return normalized


def _is_edition_remainder(remainder: str) -> bool:
    """True if the words left over after a prefix are edition/year noise.

    Two ways to qualify. Either every word is a known edition/year token,
    or the remainder simply *ends* in "edition" — which is how an
    arbitrarily-named edition ("… Marching Fire Edition", "… Gourmet
    Edition", "… Spacer's Choice Edition") is recognised without needing
    a vocabulary of every publisher's marketing word.

    The second rule is deliberately permissive, and is safe only because
    :func:`titles_match` rejects a version mismatch before ever getting
    here. Without that gate it would read "7 history edition" as noise
    and match "The Settlers" to "The Settlers 7 - History Edition".
    """
    words = remainder.split()
    if not words:
        return False
    if words[-1] == "edition":
        return True
    return all(
        w in _EDITION_TOKENS or re.fullmatch(r"(?:19|20)\d{2}", w)
        for w in words
    )


def _core_title_match(qn: str, cn: str, threshold: float) -> bool:
    """Match two already-normalised titles (no prefix/roman folding here)."""
    if qn == cn:
        return True
    qb = strip_edition_suffix(qn)
    cb = strip_edition_suffix(cn)
    if qb and qb == cb:
        return True
    # Prefix relationship whose leftover is only edition/year noise
    # (accepts "Sea of Thieves" ↔ "Sea of Thieves: 2026 Edition" but
    # rejects "Quake" ↔ "Quake II" — "ii"/"2" is not edition noise).
    for longer, shorter in ((cn, qn), (qn, cn)):
        if longer.startswith(shorter + " ") and _is_edition_remainder(
            longer[len(shorter):].strip(),
        ):
            return True
    return max(
        score_match(qn, cn), score_match(qb, cb),
    ) >= threshold


def titles_match(query: str, candidate: str, threshold: float = 0.85) -> bool:
    """Decide whether a storefront result *candidate* IS the game *query*.

    The shared accept/reject test for "given search results, which row is
    this game?" — used by the Steam ``storesearch`` resolvers feeding
    artwork, metadata, and compatibility. Designed to REJECT the wrong
    matches blind ``items[0]`` produced (sequels like *Hades* → *Hades II*,
    soundtracks like *Figment* → *Figment - Soundtrack*, unrelated hits like
    *Control* → *Steam Controller*) while still ACCEPTING legitimate
    variants:

    * ®/™/unicode/apostrophe noise (via
      :func:`~unifideck.utils.title_match.normalize_for_match`);
    * edition / year variants ("…: 2026 Edition", "Ultimate Edition");
    * publisher-prefix variants ("Splinter Cell" ↔ "Tom Clancy's Splinter
      Cell");
    * Roman/Arabic version variants ("Thief II" ↔ "Thief 2").

    Compares every combination of {raw, publisher-stripped, roman-folded}
    forms of each side; widening the forms only ever ADDS match
    opportunities, and the 0.85 Jaccard threshold still guards against
    franchise confusion. Returns ``False`` rather than guessing — callers
    prefer no data over wrong data.

    One gate runs before all of that widening: the two titles must agree
    on their :func:`~unifideck.utils.title_match.version_tokens`. Nothing
    below may bridge *Settlers 5* and *Settlers 7*, however similar the
    strings look. Measured on the edition-stripped base so an edition tag
    that happens to contain a number ("20 Year Celebration", "2026
    Edition") is not mistaken for a sequel number.
    """
    qn = normalize_for_match(query)
    cn = normalize_for_match(candidate)
    if not qn or not cn:
        return False
    if version_tokens(strip_edition_suffix(qn)) != version_tokens(
        strip_edition_suffix(cn),
    ):
        return False
    q_forms = {qn, _strip_publisher_prefix(qn)}
    q_forms |= {_fold_roman_numerals(f) for f in q_forms}
    c_forms = {cn, _strip_publisher_prefix(cn)}
    c_forms |= {_fold_roman_numerals(f) for f in c_forms}
    return any(
        _core_title_match(q, c, threshold)
        for q in q_forms
        for c in c_forms
    )
