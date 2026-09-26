"""Cross-store duplicate-card grouping — display layer only.

py_modules/unifideck/core/game_grouping.py

Unlike :mod:`cross_source_dedupe` (which *collapses* duplicates into a
single shortcut, disabled by default), this module never removes or
reorders a game. It only stamps each :class:`~unifideck.core.types.Game`
with a ``dedupe_group_id`` shared by every other copy of the same title
across stores, plus an ``edition_label`` when the title carries a
recognised edition/variant suffix. Every store's copy keeps its own real
Steam shortcut and ``app_id`` — the frontend uses the group id purely to
render one card with a multi-store badge cluster (``src/lib/game-
grouping.ts``) and a store-switcher on the detail page
(``src/lib/library-filters``'s ``getGroupSiblings``).

Wiring: ``SyncService._aggregate_results`` calls
:func:`annotate_duplicate_groups` unconditionally after the (usually
no-op) collapse step, gated by ``dedup.ui_grouping_enabled`` (default
true). It also runs on cache load and on a frontend Steam-owned-titles
push — see :mod:`unifideck.core.sync_cache_mixin` and
``rpc.mixins.sync.update_steam_owned_titles``.

Grouping vs matching
=====================
:func:`titles_match` (``utils.title_match``) answers "is this storefront
result the game I searched for" — deliberately permissive, since a
false negative there just means missing artwork. Grouping two *library
entries* onto one visible card is a different, stronger question: a
false positive here hides a real, distinct game behind another game's
tile. So grouping uses its own canonical-key equality
(:func:`_canonical_key`, built on
:func:`~unifideck.utils.title_match.strip_edition_suffix_for_grouping`)
rather than the fuzzy ``titles_match`` — see that function's docstring
for exactly which suffixes it refuses to strip (remasters, remakes,
"Legendary"/"Classic" tags, and — unlike edition-suffix stripping
elsewhere — trailing years, since "Dead Space" and "Dead Space (2023)"
must never share a card).

Grouping by exact canonical-key equality also sidesteps the transitive-
closure trap a pairwise union-find has: with fuzzy pairwise matching,
A~B and B~C does not imply A~C (BioShock ~ BioShock Infinite via a loose
threshold, BioShock ~ BioShock Remastered via another, chaining three
different products into one card even though BioShock Infinite and
BioShock Remastered don't match each other). Grouping by a single
shared key can't chain — every member is directly equal to the same
canonical key, not just to some other member.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from unifideck.utils.title_match import (
    _strip_publisher_prefix,
    extract_edition_label,
    leftover_word_count,
    normalize_for_match,
    strip_edition_suffix_for_grouping,
    titles_match,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from unifideck.config import ConfigManager
    from unifideck.steam.owned_games import OwnedApp

    from .types import Game


def _canonical_key(title: str) -> str:
    """Grouping identity for ``title`` — same product, any store/edition.

    Publisher-prefix-stripped, then edition-suffix-stripped with the
    *grouping* stripper (:func:`strip_edition_suffix_for_grouping`),
    which — unlike the general-purpose stripper — refuses to fold away
    remasters, remakes, "Legendary"/"Classic" tags, or a trailing year,
    since each of those names a distinct product for grouping purposes.

    Used both as the grouping equality key and, directly, as the
    ``dedupe_group_id`` itself (see module docstring) — order-
    independent by construction, unlike a union-find root.
    """
    normalized = normalize_for_match(title)
    if not normalized:
        return ""
    normalized = _strip_publisher_prefix(normalized)
    return strip_edition_suffix_for_grouping(normalized)


def _group_by_canonical_key(games: Sequence[Game]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for index, game in enumerate(games):
        key = _canonical_key(game.title)
        if not key:
            continue
        groups.setdefault(key, []).append(index)
    return groups


def _assign_group_ids(
    games: list[Game], groups: dict[str, list[int]],
) -> None:
    for key, indices in groups.items():
        group_id = key if len(indices) > 1 else None
        for i in indices:
            games[i].dedupe_group_id = group_id
            games[i].edition_label = extract_edition_label(games[i].title)
    # Titles with no canonical key (empty/unnormalisable) never entered
    # ``groups`` at all — they still need their (always-None) edition
    # label stamped so every game leaves this function fully annotated.
    grouped_indices = {i for indices in groups.values() for i in indices}
    for i, game in enumerate(games):
        if i not in grouped_indices:
            game.dedupe_group_id = None
            game.edition_label = extract_edition_label(game.title)


def annotate_duplicate_groups(
    games: Sequence[Game],
    *,
    steam_owned: Mapping[str, OwnedApp] | None = None,
) -> list[Game]:
    """Stamp ``dedupe_group_id`` / ``edition_label`` on every game.

    Pure and order-preserving: same length, same order, same objects
    (mutated in place) as the input. A group of size 1 gets
    ``dedupe_group_id = None`` — there is nothing to visually merge, and
    ``None`` lets the frontend treat "ungrouped" as its own singleton
    group without a special case.

    Grouping equality is :func:`_canonical_key`, not the fuzzy
    ``titles_match`` used elsewhere in this codebase — see the module
    docstring for why grouping needs a stricter, non-transitive rule.
    The group id **is** the shared canonical key, so it's stable
    regardless of input order (no union-find root to depend on).

    ``steam_owned`` (``{normalized title: OwnedApp}``, from
    ``steam.owned_games.get_all_owned_app_ids``) is optional — when
    given, every game whose title matches an entry also gets
    ``steam_owned_app_id`` (and ``steam_owned_edition_label``, extracted
    from the Steam copy's own original title) set, independent of
    ``dedupe_group_id``. This is how a duplicate group (or even a
    singleton title) learns the user already owns the same game on real
    Steam, which Unifideck's own sync never sees since it only ever
    aggregates Epic/GOG/Amazon/Ubisoft/Battle.net/Microsoft.
    """
    games = list(games)
    groups = _group_by_canonical_key(games)
    _assign_group_ids(games, groups)

    if steam_owned:
        _annotate_steam_owned(games, steam_owned)

    return games


def annotate_duplicate_groups_if_enabled(
    games: list[Game], config: ConfigManager | None,
) -> list[Game]:
    """:func:`annotate_duplicate_groups`, gated by
    ``dedup.ui_grouping_enabled`` (default true) and resolving the
    Steam-owned cross-reference itself.

    Single entry point for every call site that needs the
    config-gate + Steam-owned-lookup wiring, not just the pure
    annotation — used after a fresh sync
    (``sync_results_mixin._maybe_annotate_duplicate_groups``), on
    ``library_cache.json`` load (``sync_cache_mixin._load_library_cache``,
    C.10 — an upgrading user's cached games otherwise carry no group
    annotation until their next sync), and after the frontend pushes a
    fresh Steam-owned-titles snapshot
    (``rpc.mixins.sync.update_steam_owned_titles``, C.11).

    A ``get_all_owned_app_ids`` failure degrades to "no Steam
    cross-referencing" rather than failing the whole call — annotation
    of cross-store groups (which needs no Steam data) still happens.
    """
    from unifideck.utils.config_helpers import get_cfg

    if not get_cfg(config, "dedup.ui_grouping_enabled", True):
        return games

    from unifideck.steam.owned_games import get_all_owned_app_ids

    try:
        steam_owned = get_all_owned_app_ids(config)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "[game_grouping] get_all_owned_app_ids failed — "
            "continuing without Steam-owned cross-referencing",
        )
        steam_owned = {}
    return annotate_duplicate_groups(games, steam_owned=steam_owned)


def _bucket_steam_owned(
    steam_owned: Mapping[str, OwnedApp],
) -> dict[str, list[OwnedApp]]:
    """Bucket every owned app by the first *grouping-consistent* word of
    its own (original, non-normalised) title.

    Bucketing on ``owned_app.title`` — run through the same
    ``normalize_for_match`` every other title in this module uses —
    rather than on ``steam_owned``'s dict keys. Those keys come from
    ``unifidb.normalize_title_for_matching``, a *different* normaliser
    that drops apostrophes entirely (``"Assassin's"`` → ``"assassins"``)
    where ``normalize_for_match`` turns them into a space
    (``"assassin s"``). Bucketing by the dict key meant an
    apostrophe'd title's first-word bucket never matched
    ``_bucket_keys(game.title)``'s, silently dropping the Steam
    cross-reference for every such title (Assassin's Creed, The Bard's
    Tale, …) — bucketing by the original title through the shared
    normaliser fixes that at the source, for every consumer of this
    bucket.
    """
    owned_buckets: dict[str, list[OwnedApp]] = {}
    seen_appids: set[int] = set()
    for owned_app in steam_owned.values():
        if owned_app.appid in seen_appids:
            continue
        seen_appids.add(owned_app.appid)
        normalized_title = normalize_for_match(owned_app.title)
        if not normalized_title:
            continue
        owned_buckets.setdefault(normalized_title.split()[0], []).append(
            owned_app,
        )
    return owned_buckets


def _dedupe_bucket_candidates(
    title: str,
    owned_buckets: dict[str, list[OwnedApp]],
) -> list[OwnedApp]:
    """Every owned app reachable from any of ``title``'s bucket keys, each
    appid included at most once — a title can hit more than one bucket
    (see :func:`_bucket_keys`), and a candidate common to two of them
    must not be scored/compared twice in :func:`_find_steam_owned_match`.
    """
    candidates: list[OwnedApp] = []
    seen_appids: set[int] = set()
    for key in _bucket_keys(title):
        for owned_app in owned_buckets.get(key, []):
            if owned_app.appid in seen_appids:
                continue
            seen_appids.add(owned_app.appid)
            candidates.append(owned_app)
    return candidates


def _steam_owned_match_rank(
    game_title: str,
    query_norm: str,
    query_base: str,
    owned_app: OwnedApp,
) -> tuple[int, int] | None:
    """``(tier, leftover words)`` for one candidate — lower sorts better
    — or ``None`` when it doesn't match at all. Split out of
    :func:`_find_steam_owned_match` purely to keep that function's own
    branching within the repo's complexity budget; the three tiers
    (exact > edition-stripped > fuzzy) are documented on that function.
    """
    from unifideck.utils.title_match import strip_edition_suffix

    candidate_norm = normalize_for_match(owned_app.title)
    if not candidate_norm:
        return None
    if candidate_norm == query_norm:
        tier = 0
    elif strip_edition_suffix(candidate_norm) == query_base:
        tier = 1
    elif titles_match(game_title, owned_app.title):
        tier = 2
    else:
        return None
    return (tier, leftover_word_count(query_norm, candidate_norm))


def _find_steam_owned_match(
    game: Game,
    owned_buckets: dict[str, list[OwnedApp]],
) -> OwnedApp | None:
    """Best Steam-owned match for ``game``, not the first bucket hit.

    A game can fall in the same bucket as several owned Steam titles
    (e.g. "BioShock Infinite" buckets with both "BioShock" and
    "BioShock Infinite"); returning whichever came first in dict-
    iteration order silently picked the wrong appid depending on
    insertion order. Scored instead (see :func:`_steam_owned_match_rank`):
    exact ``normalize_for_match`` equality wins outright, then equality
    after ``strip_edition_suffix`` (handles "Thief Gold" ↔ owned "Thief
    Gold" vs. the unrelated "Thief" also in-bucket), then the general
    fuzzy ``titles_match`` acceptance — and among several
    ``titles_match`` acceptances, the fewest leftover words relative to
    the query wins (picks exact "BioShock Infinite" over "BioShock" for
    a query of "BioShock Infinite").
    """
    from unifideck.utils.title_match import strip_edition_suffix

    query_norm = normalize_for_match(game.title)
    if not query_norm:
        return None
    query_base = strip_edition_suffix(query_norm)

    best: OwnedApp | None = None
    best_rank = (4, 0)  # (tier, leftover words) — lower is better
    for owned_app in _dedupe_bucket_candidates(game.title, owned_buckets):
        rank = _steam_owned_match_rank(game.title, query_norm, query_base, owned_app)
        if rank is not None and rank < best_rank:
            best_rank = rank
            best = owned_app
    return best


def _bucket_keys(title: str) -> set[str]:
    """First normalised word, plus the publisher-prefix-stripped first
    word when a known prefix is present.

    A single first-word bucket would miss the exact case
    ``titles_match`` is designed to accept — "Splinter Cell" vs "Tom
    Clancy's Splinter Cell" share no first word. Adding the
    prefix-stripped variant as a second bucket key costs nothing (the
    prefix table is 10 entries) and keeps the O(N + bucket^2) shape,
    since only titles that actually carry one of these prefixes gain an
    extra bucket membership.
    """
    from unifideck.utils.title_match import PUBLISHER_PREFIXES

    normalized = normalize_for_match(title)
    if not normalized:
        return {""}
    keys = {normalized.split()[0]}
    for prefix in PUBLISHER_PREFIXES:
        if normalized.startswith(prefix + " "):
            stripped = normalized[len(prefix) :].strip()
            if stripped:
                keys.add(stripped.split()[0])
            break
    return keys


def _annotate_steam_owned(
    games: list[Game], steam_owned: Mapping[str, OwnedApp],
) -> None:
    """Set ``steam_owned_app_id``/``steam_owned_edition_label`` on every
    game matching a Steam title.

    Bucketed by :func:`_bucket_steam_owned` (keyed via the same
    normaliser this module uses everywhere else — see that function's
    docstring for why the raw ``steam_owned`` dict keys can't be used
    directly), then resolved via :func:`_find_steam_owned_match`'s
    best-match scoring rather than a first-hit lookup.

    The edition label comes from the Steam copy's own original title
    (``OwnedApp.title``), via the same :func:`extract_edition_label`
    used for every other store — NOT copied from whichever Unifideck
    game matched it, since title-matching tolerates edition differences
    on purpose (that's how a base game groups with its "Ultimate
    Edition" copy) and assuming they're the same edition would just
    trade one wrong label for another.
    """
    owned_buckets = _bucket_steam_owned(steam_owned)
    for game in games:
        owned_app = _find_steam_owned_match(game, owned_buckets)
        if owned_app is None:
            continue
        game.steam_owned_app_id = owned_app.appid
        game.steam_owned_edition_label = extract_edition_label(owned_app.title)
