"""Edition/platform/variant suffix tables and stripping.

Split out of the former monolithic ``title_match.py`` (2026-09-26, file-size
gate). Two stripping strategies live here side by side because they answer
different questions:

* :func:`strip_edition_suffix` — "what's the base title", for *matching*
  (artwork/metadata lookup), where accepting "same game, different
  edition/remaster/year" is exactly the point.
* :func:`strip_edition_suffix_for_grouping` — the same question for
  cross-store duplicate *grouping*, which is a stronger claim: a remaster,
  remake, or differently-numbered yearly release is a distinct product a
  player owns and plays separately, so :data:`GROUPING_UNSAFE_SUFFIXES` and
  a trailing year must survive here even though matching folds them away.
"""
from __future__ import annotations

import re

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
    "digital version",
    # Short / standalone (word boundary ensured by space-prefix check).
    # Deliberately excludes bare "revolution" and "console": both are
    # common LAST WORDS of real, unrelated game titles ("Cyber
    # Revolution", "Duel Revolution", "Creative Console" all appeared in
    # a live-library audit getting their real title word eaten as a
    # fake "edition"), unlike "goty"/"hd"/"ce"/"dlc", which are genuine
    # edition/release jargon with no similar false-positive found.
    "goty", "hd", "ce", "dlc", "windows", "xs",
)


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
    ``matching._is_edition_remainder``, which reads them as edition
    noise when comparing a base game against its edition, and which is
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

# Same as _STRIP_STRATEGIES, minus _strip_trailing_year — used only to
# find extract_edition_label's split point. Matching (strip_edition_suffix)
# treats a trailing year as disposable so "Flight Simulator 2020" fuzzy-
# matches "...2024"; the label extractor must not inherit that, or a
# year that's genuinely part of the title ends up glued onto the label
# instead of the base ("Microsoft Flight Simulator 2024 - Standard
# Edition" → base "microsoft flight simulator", label "2024 - Standard
# Edition" — the "2024" leaks in because it strips before "Standard
# Edition" does in the full iterate-to-fixpoint loop). Dropping the
# year-stripper from this variant keeps a trailing year attached to the
# base for splitting purposes, whatever else is stripped around it.
_LABEL_SPLIT_STRIP_STRATEGIES = (
    _strip_known_suffix,
    _strip_edition_phrase,
    _strip_chapters_episodes,
    _strip_celebration,
)


def strip_edition_suffix_for_label_split(normalized: str) -> str:
    """Like :func:`strip_edition_suffix`, but never strips a trailing
    year — see :data:`_LABEL_SPLIT_STRIP_STRATEGIES`. Used only by
    ``edition_label.extract_edition_label``."""
    changed = True
    while changed:
        changed = False
        for strip in _LABEL_SPLIT_STRIP_STRATEGIES:
            stripped = strip(normalized)
            if stripped and stripped != normalized:
                normalized = stripped
                changed = True
                break
    return normalized


# Suffixes that ``titles_match`` (artwork/metadata resolution) tolerates
# as "same game, different release", but which ``core.game_grouping``
# must NOT dissolve — grouping "Dead Space" with "Dead Space
# (2023)" would collapse a remake and its 2008 original into one card,
# hiding a real game behind the other's tile. Everything else in
# ``EDITION_SUFFIXES`` (platform tags, "Deluxe/Ultimate/GOTY Edition",
# etc.) really is the same product re-skinned and stays safe to strip.
#
# "Definitive Edition" belongs here too, alongside "Remastered"/"Remake":
# publishers use it for both meanings, and grouping can't tell them apart
# from the string alone —
#   - a distinct remaster with its own store page and appid (Dishonored
#     - Definitive Edition, THIEF: Definitive Edition, Tomb Raider:
#     Definitive Edition, Ori and the Blind Forest: Definitive Edition —
#     each sits next to an unsuffixed original in real libraries), vs.
#   - a publisher's only current listing for an older game (Mafia:
#     Definitive Edition, Gamedec - Definitive Edition), where there is
#     no separate unsuffixed release to conflict with.
# Refusing to strip it costs nothing in the second case (the title just
# stays an ungrouped singleton, same as today) and fixes real over-
# merging in the first — see the audit that found Dishonored, Thief and
# Tomb Raider all wrongly sharing a card with their Definitive Edition.
GROUPING_UNSAFE_SUFFIXES: frozenset[str] = frozenset({
    "remastered", "remake", "directors cut", "the final cut",
    "classic edition", "legendary edition", "definitive edition",
})


def _strip_known_suffix_for_grouping(s: str) -> str | None:
    """Like :func:`_strip_known_suffix`, minus :data:`GROUPING_UNSAFE_SUFFIXES`."""
    for suffix in EDITION_SUFFIXES:
        if suffix in GROUPING_UNSAFE_SUFFIXES:
            continue
        if s.endswith(" " + suffix):
            stripped = s[: -(len(suffix) + 1)].strip()
            if stripped:
                return stripped
    return None


# Grouping never strips a trailing year (a differing year names a
# different release — "Flight Simulator 2020" vs "…2024", "Battlefront
# 2005" vs "…2017") or an anniversary/celebration phrase (same
# reasoning), unlike :func:`strip_edition_suffix`'s full strategy list.
_GROUPING_STRIP_STRATEGIES = (
    _strip_known_suffix_for_grouping,
    _strip_edition_phrase,
    _strip_chapters_episodes,
)


def strip_edition_suffix_for_grouping(normalized: str) -> str:
    """Edition-suffix stripping for cross-store duplicate *grouping* only.

    Deliberately more conservative than :func:`strip_edition_suffix`:
    that function widens matching for artwork/metadata lookup, where
    accepting "same game, different edition/remaster/year" is exactly
    the point. Grouping two library entries onto one visible card is a
    stronger claim — a remaster, remake, or differently-numbered yearly
    release is a distinct product a player owns and plays separately,
    so those must survive here even though they're deliberately folded
    away for artwork purposes. See :data:`GROUPING_UNSAFE_SUFFIXES` and
    the trailing-year/celebration omission above.

    Pure function; same iterate-to-fixpoint shape as
    :func:`strip_edition_suffix`.
    """
    changed = True
    while changed:
        changed = False
        for strip in _GROUPING_STRIP_STRATEGIES:
            stripped = strip(normalized)
            if stripped and stripped != normalized:
                normalized = stripped
                changed = True
                break
    return normalized
