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
not a version (see :func:`_strip_chapters_episodes`) — episodes of one
season share their season's art on purpose.
"""
from __future__ import annotations

import re
import unicodedata

# 58-entry suffix table, longest-first within each group so
# "xbox series xs edition" gets stripped before "xbox edition" /
# "edition" alone. The iterative outer loop in ``strip_edition_suffix``
# restarts after each strip so compound suffixes work end-to-end
# (e.g. "X Standard Edition Windows" → strip Windows → strip
# Standard Edition → "X").
EDITION_SUFFIXES: tuple[str, ...] = (
    # Platform / console suffixes
    "xbox series xs edition", "xbox one edition", "xbox edition",
    "xbox series xs", "xbox one version", "xbox one",
    "pc edition", "windows 10 edition", "windows edition",
    "console edition",
    "for pc", "for windows", "for xbox",
    # Distribution / bundle suffixes
    "cross gen bundle", "cross gen edition", "game preview",
    "the complete season", "the complete first season",
    # Full edition names
    "deluxe edition", "gold edition", "ultimate edition",
    "complete edition", "goty edition", "game of the year edition",
    "definitive edition", "enhanced edition", "special edition",
    "anniversary edition", "premium edition", "standard edition",
    "legacy edition", "collectors edition", "limited edition",
    "digital edition", "classic edition", "royal edition",
    "legendary edition", "elite edition", "ea play edition",
    "remastered", "remake", "directors cut", "the final cut",
    "unofficial patch",
    "revolution",
    "digital version",
    # Short / standalone (word boundary ensured by space-prefix check)
    "goty", "hd", "ce", "dlc", "windows", "console", "xs",
)

# Multi-character Roman numerals → Arabic, for version folding ("Thief II"
# ↔ "Thief 2"). Single letters (I/V/X) are deliberately excluded — they're
# too often branding ("Mega Man X", "Entropy Effect X") rather than a
# version, and folding them risks false matches.
_ROMAN_TO_ARABIC: dict[str, str] = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
    "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
}


def version_tokens(normalized: str) -> frozenset[str]:
    """Sequel discriminators in a normalised title, folded to Arabic.

    The *one* part of a title that must survive every suffix strip. Two
    titles that differ only here are different games, so the strippers
    below refuse to consume these and the resolvers refuse to match
    across a mismatch.

    Counts multi-character Roman numerals (via ``_ROMAN_TO_ARABIC``, so
    "II" and "2" agree) and bare integers. Deliberately NOT counted:

    * 4-digit years 1980-2030 — an edition tag, not a sequel number, so
      "Sea of Thieves" still matches "Sea of Thieves: 2026 Edition".
      Anno's 1404 / 1701 / 1602 fall outside that window and DO count,
      which is exactly right: they name different games.
    * ordinals ("10th") and any token with non-digit characters — those
      are words, not version markers.

    Pure function. Empty input, or a title with no version marker,
    returns an empty set (which is itself a value: "no number" differs
    from "number 7", so ``The Settlers`` won't match ``The Settlers 7``).
    """
    tokens: set[str] = set()
    for word in normalized.split():
        roman = _ROMAN_TO_ARABIC.get(word)
        if roman is not None:
            tokens.add(roman)
        elif word.isdigit() and not (
            len(word) == 4 and 1980 <= int(word) <= 2030
        ):
            tokens.add(str(int(word)))
    return frozenset(tokens)


def normalize_for_match(title: str) -> str:
    """Lowercase + strip symbols + collapse whitespace.

    Steps in order:

    1. lowercase + trim;
    2. dual-language "Game / Jeu" → first half;
    3. ® ™ © → space (preserves word boundaries: ``Watch Dogs®2``
       becomes ``watch dogs 2`` not ``watch dogs2``);
    4. NFKD-decompose + strip combining marks (é→e, ü→u);
    5. strip ``(TM)`` ``(R)`` ``(C)``;
    6. ``&`` → ``and``;
    7. smart-quotes → ASCII;
    8. ``_`` / ``-`` → space;
    9. ``|`` → empty (so ``X|S`` becomes ``XS`` not ``X S``);
    10. remaining punctuation → space;
    11. collapse runs of whitespace.

    Returns the normalised string. Empty input returns empty string.
    """
    if not title:
        return ""
    t = title.lower().strip()
    if " / " in t:
        t = t.split(" / ", 1)[0].strip()
    t = re.sub(r"[®™©]", " ", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\((?:tm|r|c)\)", "", t, flags=re.IGNORECASE)
    t = t.replace("&", " and ")
    t = t.replace("‘", "'").replace("’", "'")  # noqa: RUF001  smart-quote match is intentional
    t = t.replace("“", '"').replace("”", '"')
    t = t.replace("_", " ").replace("-", " ").replace("|", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_edition_suffix(normalized: str) -> str:
    """Iteratively strip edition / platform / variant suffixes.

    Repeats until no more suffixes match so compound cases work:

        "call of duty black ops 6 standard edition windows"
            → strip "windows" → "...standard edition"
            → strip "standard edition" → "call of duty black ops 6"

    Also handles three generic patterns after the explicit table is
    exhausted:

    * any ``<1-3 words> edition`` ending (catches "marching fire
      edition", "ultimate survivor edition", etc.);
    * ``chapters/episodes <range>`` endings;
    * trailing 4-digit years between 1980-2030.

    Pure function. Always returns at least the first word (won't
    return empty even if the input was entirely suffixes).
    """
    changed = True
    while changed:
        changed = False
        for strip in _STRIP_STRATEGIES:
            stripped = strip(normalized)
            if stripped and stripped != normalized:
                normalized = stripped
                changed = True
                break
    return normalized


def _strip_known_suffix(s: str) -> str | None:
    """Strip one entry from the explicit ``EDITION_SUFFIXES`` table."""
    for suffix in EDITION_SUFFIXES:
        if s.endswith(" " + suffix):
            stripped = s[: -(len(suffix) + 1)].strip()
            if stripped:
                return stripped
    return None


def _strip_edition_phrase(s: str) -> str | None:
    """Strip the bare trailing ``edition`` word.

    This used to consume ``<1-2 words> edition`` via a non-greedy
    ``(.+?)``, which meant it removed as MUCH as it could rather than as
    little. Nothing distinguishes an edition qualifier from an ordinary
    title word by position, so it ate both:

        "sea of thieves 2026 edition"          → "sea of"
        "halo the master chief collection ed…" → "halo the master"
        "the settlers 7 history edition"       → "the settlers"

    The last one is why a tester saw one cover on seven Settlers games:
    a sequel number is exactly one word, so every "<game> N - <word>
    Edition" collapsed to the bare franchise on both the query and the
    candidate side.

    Removing only the ``edition`` token cannot destroy a title word.
    The multi-word edition names this used to absorb ("Marching Fire
    Edition", "Gourmet Edition") are still recognised — by
    :func:`_is_edition_remainder`, which reads them as edition noise
    when comparing a base game against its edition, and which is
    version-gated so it cannot bridge two different sequels.
    """
    m = re.match(r"^(.+)\s+edition$", s)
    if not m or not m.group(1).strip():
        return None
    return m.group(1).strip()


def _strip_chapters_episodes(s: str) -> str | None:
    """Strip a trailing ``chapters/episodes <range>`` suffix.

    Deliberately NOT version-guarded, unlike :func:`_strip_edition_phrase`.
    An episode number is not a sequel number: "A New Frontier - Episode 1"
    and "… Episode 2" are parts of one season, and storefronts carry a
    single artwork entry for the season. Collapsing them to a shared base
    is the correct answer, so an episodic shortcut inherits the season's
    art instead of having none.
    """
    m = re.match(r"^(.+?)\s+(?:chapters?|episodes?)\s+[\d\s]+$", s)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _strip_celebration(s: str) -> str | None:
    """Strip a trailing anniversary/celebration suffix.

    Catches "Rise of the Tomb Raider: 20 Year Celebration" →
    "rise of the tomb raider", "<game> anniversary celebration", and
    "<game> celebration". These re-release tags aren't in the explicit
    edition table and aren't "<word> edition", so they slipped through
    and caused the base title to be rejected against the Steam hit.
    """
    m = re.match(
        r"^(.+?)\s+(?:\d+\s+)?(?:year\s+)?(?:anniversary\s+)?celebration$", s,
    )
    return m.group(1).strip() if m and m.group(1).strip() else None


def _strip_trailing_year(s: str) -> str | None:
    """Strip a trailing 4-digit year in the 1980-2030 range."""
    m = re.match(r"^(.+?\D)\s+(\d{4})$", s)
    if m and 1980 <= int(m.group(2)) <= 2030 and m.group(1).strip():
        return m.group(1).strip()
    return None


# Ordered strip strategies. ``strip_edition_suffix`` applies them
# repeatedly (re-trying from the top after each change) so compound
# suffixes peel off one layer at a time.
_STRIP_STRATEGIES = (
    _strip_known_suffix,
    _strip_edition_phrase,
    _strip_chapters_episodes,
    _strip_celebration,
    _strip_trailing_year,
)


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


def clean_search_query(title: str) -> str:
    """Pre-API query cleanup — strip noise that hurts SGDB autocomplete.

    Different from :func:`normalize_for_match` — that one prepares a
    string for *comparison*; this one prepares a string for *sending
    to the API*. SGDB autocomplete is forgiving but does worse on
    titles with platform/edition suffixes attached because it returns
    the first substring match and edition-tagged entries sort later.

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


def _fold_roman_numerals(normalized: str) -> str:
    """Fold multi-char Roman-numeral tokens to Arabic in a normalised title."""
    return " ".join(_ROMAN_TO_ARABIC.get(w, w) for w in normalized.split())


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

    * ®/™/unicode/apostrophe noise (via :func:`normalize_for_match`);
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
    on their :func:`version_tokens`. Nothing below may bridge *Settlers
    5* and *Settlers 7*, however similar the strings look. Measured on
    the edition-stripped base so an edition tag that happens to contain
    a number ("20 Year Celebration", "2026 Edition") is not mistaken for
    a sequel number.
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
