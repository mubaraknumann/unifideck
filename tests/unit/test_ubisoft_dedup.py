"""Unit tests for Ubisoft parent/edition DLC dedup in ``_GameBuilder``.

Ported from staging's ``known_base_names`` / ``all_db_names`` ownership
dedup (staging ``ubisoft.py`` ~L2180-2340). The refactor seeds the base
set from the kept configs (no GraphQL) and from the community game-ID DB.

Three behaviours are covered:

1. ``"Parent - DLC"`` collapses when ``Parent`` is an owned base game
   or appears in the community DB.
2. ``"Parent: Subtitle"`` collapses only under catalog gating: the base
   must be a known Algolia game *and* separately owned, and the full
   title must be absent from the catalog. Standalone subtitled games
   (Prince of Persia: The Sands of Time, Watch Dogs: Legion) are catalog
   entries in their own right, so they survive.
3. Edition variants ("Gold Edition") collapse onto the plain base title.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.library.game_builder import _GameBuilder
from unifideck.stores.ubisoft.library.identity_resolver import _IdentityResolver
from unifideck.stores.ubisoft.parser import GameConfig


def _norm(name: str) -> str:
    """Reuse the production normaliser so tests track its behaviour."""
    return UbisoftIdMap._normalize_for_matching(name)


class _IdMap:
    """id_map double exposing only what ``_GameBuilder`` touches."""

    def __init__(self) -> None:
        self.bulk: dict[str, dict[str, Any]] = {}

    def normalize_for_matching(self, name: str) -> str:
        return _norm(name)

    def update_bulk(self, mapping: dict[str, dict[str, Any]]) -> None:
        self.bulk.update(mapping)

    def reconcile_connect_ids(
        self, fresh: dict[str, str], space_ids: list[str],
    ) -> None:
        for space_id in space_ids:
            connect_id = fresh.get(space_id)
            if connect_id:
                self.bulk.setdefault(space_id, {})["ubisoftconnect_game_id"] = (
                    connect_id
                )


def _cfg(install_id: int, space_id: str, name: str) -> GameConfig:
    c = GameConfig()
    c.install_id = install_id
    c.launch_id = install_id
    c.space_id = space_id
    c.name = name
    return c


def _builder() -> _GameBuilder:
    return _GameBuilder(config=object(), id_map=_IdMap())


def _titles(games: list[Any]) -> set[str]:
    return {g.title for g in games}


def test_named_expansion_with_dash_is_dropped():
    """"Base - Expansion" collapses when the base game is owned."""
    base = _cfg(1, "s1", "Assassin's Creed Valhalla")
    dlc = _cfg(2, "s2", "Assassin's Creed Valhalla - Dawn of Ragnarok")
    games = _builder().build_games_from_configs(
        [base, dlc], installed={},
    )
    assert _titles(games) == {"Assassin's Creed Valhalla"}


def test_dash_parent_from_db_only_is_dropped():
    """A " - " DLC collapses when the parent is only in the community DB."""
    dlc = _cfg(2, "s2", "Watch Dogs Legion - Season Pass Extra")
    db_names = {_norm("Watch Dogs Legion")}
    games = _builder().build_games_from_configs(
        [dlc], installed={}, db_names=db_names,
    )
    # "season pass" also trips the keyword filter, so prove the parent
    # path with a keyword-clean subtitle:
    dlc2 = _cfg(3, "s3", "Watch Dogs Legion - Bloodline Story")
    games2 = _builder().build_games_from_configs(
        [dlc2], installed={}, db_names=db_names,
    )
    assert games == []
    assert games2 == []


def test_colon_standalone_title_survives():
    """"Parent: Subtitle" is kept when no base "Parent" is owned."""
    game = _cfg(1, "s1", "Prince of Persia: The Sands of Time")
    db_names = {_norm("Prince of Persia")}  # DB has it; must NOT matter
    games = _builder().build_games_from_configs(
        [game], installed={}, db_names=db_names,
    )
    assert _titles(games) == {"Prince of Persia: The Sands of Time"}


def test_colon_sequel_not_dropped_even_when_prefix_owned():
    """A standalone "Franchise: Subtitle" survives even if the prefix is owned.

    This is the deliberate divergence from staging: colon parent-matching
    is unsafe without GraphQL pre-filtering, so owning *Watch Dogs* must
    NOT delete the standalone *Watch Dogs: Legion*.
    """
    base = _cfg(1, "s1", "Watch Dogs")
    sequel = _cfg(2, "s2", "Watch Dogs: Legion")
    games = _builder().build_games_from_configs(
        [base, sequel], installed={},
    )
    assert _titles(games) == {"Watch Dogs", "Watch Dogs: Legion"}


def test_edition_variant_kept_separate_from_base():
    """Base and edition are distinct entries when both are owned.

    Editions are real games, shown under their own name — they are not
    collapsed onto a base the user may not even own. A genuine *duplicate*
    (same base, same edition) still collapses; see the canonical-identity
    tests below.
    """
    base = _cfg(1, "s1", "Far Cry 6")
    gold = _cfg(2, "s2", "Far Cry 6 Gold Edition")
    games = _builder().build_games_from_configs(
        [base, gold], installed={},
    )
    assert _titles(games) == {"Far Cry 6", "Far Cry 6 Gold Edition"}


def test_lone_edition_is_kept():
    """An edition with no plain base present is still shown."""
    gold = _cfg(1, "s1", "Far Cry 6 Gold Edition")
    games = _builder().build_games_from_configs([gold], installed={})
    assert _titles(games) == {"Far Cry 6 Gold Edition"}


def test_distinct_games_not_collapsed():
    """Sequels / distinct titles are never merged."""
    a = _cfg(1, "s1", "Assassin's Creed")
    b = _cfg(2, "s2", "Assassin's Creed II")
    games = _builder().build_games_from_configs([a, b], installed={})
    assert _titles(games) == {"Assassin's Creed", "Assassin's Creed II"}


def test_history_edition_not_treated_as_dlc():
    """"Anno 1602 - History Edition" is a game, not DLC of "Anno 1602".

    Regression for the missing-game report: the " - " separator + a base
    title present in the community DB previously dropped the edition.
    """
    he = _cfg(16236, "", "Anno 1602 - History Edition")
    db_names = {_norm("Anno 1602")}  # base present in the legacy list
    games = _builder().build_games_from_configs(
        [he], installed={}, db_names=db_names,
    )
    assert _titles(games) == {"Anno 1602 - History Edition"}


def test_base_and_edition_both_shown_when_both_owned():
    """Owning both the base and an edition surfaces both, distinctly."""
    base = _cfg(3621, "", "Anno 1602")
    he = _cfg(16236, "", "Anno 1602 - History Edition")
    catalog = {_norm("Anno 1602")}
    games = _builder().build_games_from_configs(
        [base, he], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"Anno 1602", "Anno 1602 - History Edition"}


def test_cross_namespace_publisher_prefix_collapses():
    """Same game under a UUID and a publisher-prefixed legacy name = one entry.

    The UUID entry ("The Division 2") and the legacy entry ("Tom Clancy's
    The Division 2") resolve to the same catalog base game and collapse;
    the UUID wins the deterministic ``store_game_id`` selection.
    """
    uuid_game = _cfg(0, "uuid-div2", "The Division 2")
    legacy = _cfg(555, "", "Tom Clancy's The Division 2")
    catalog = {_norm("The Division 2")}
    games = _builder().build_games_from_configs(
        [uuid_game, legacy], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"The Division 2"}
    assert games[0].store_game_id == "uuid-div2"


def test_legacy_id_variants_collapse_to_lowest_id():
    """One game owned under several legacy ids → one entry, lowest id wins."""
    games = _builder().build_games_from_configs(
        [
            _cfg(61350, "", "Anno 1602"),
            _cfg(3621, "", "Anno 1602"),
            _cfg(10322, "", "Anno 1602"),
        ],
        installed={},
    )
    assert _titles(games) == {"Anno 1602"}
    assert games[0].store_game_id == "3621"


def test_sequel_not_collapsed_into_base_via_suffix():
    """Suffix canonicalisation must not fold "X II" into "X"."""
    catalog = {_norm("Assassin's Creed")}
    games = _builder().build_games_from_configs(
        [
            _cfg(1, "s1", "Assassin's Creed"),
            _cfg(2, "s2", "Assassin's Creed II"),
        ],
        installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"Assassin's Creed", "Assassin's Creed II"}


def test_dlc_and_noise_rows_dropped():
    """Season passes and legacy-list noise never become shortcuts."""
    games = _builder().build_games_from_configs(
        [
            _cfg(1, "", "Far Cry 6 - Season Pass"),
            _cfg(2, "", "Subscription - Anno 1602"),
            _cfg(3, "", "Anno 1602 History Edition Company Logo"),
        ],
        installed={},
    )
    assert games == []


def test_third_party_steam_copy_is_dropped():
    """A config UPC marks as a Steam copy never becomes a Ubisoft shortcut."""
    steam_copy = _cfg(1005, "s1", "Far Cry Primal")
    steam_copy.third_party_platform = "Steam"
    native = _cfg(1, "s2", "Anno 1800")
    games = _builder().build_games_from_configs(
        [steam_copy, native], installed={},
    )
    assert _titles(games) == {"Anno 1800"}


def test_catalog_known_game_survives_keyword_filter():
    """A catalog base game whose title contains a DLC-ish word is kept."""
    game = _cfg(1, "", "Trackmania Club Access")  # "club" trips the filter
    catalog = {_norm("Trackmania Club Access")}
    games = _builder().build_games_from_configs(
        [game], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"Trackmania Club Access"}


def test_colon_dlc_dropped_when_base_catalog_known_and_owned():
    """Catalog-gated colon rule drops "Base: Subtitle" DLC.

    The base ("Trials Fusion") is a known catalog game the user also owns
    separately, and the full DLC title is absent from the catalog — the
    exact shape of the leaking Trials Fusion expansions.
    """
    base = _cfg(733, "", "Trials Fusion")
    dlc = _cfg(671, "", "Trials Fusion: Riders of the Rustlands")
    catalog = {_norm("Trials Fusion")}
    games = _builder().build_games_from_configs(
        [base, dlc], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"Trials Fusion"}


def test_colon_subtitled_game_survives_when_itself_catalog_known():
    """A subtitled game that is itself in the catalog is never dropped.

    "Prince of Persia: The Sands of Time" is a catalog base game in its
    own right, so even with base "Prince of Persia" owned + catalogued it
    stays (the full title is present in the catalog).
    """
    base = _cfg(276, "", "Prince of Persia")
    sot = _cfg(111, "", "Prince of Persia: The Sands of Time")
    catalog = {
        _norm("Prince of Persia"),
        _norm("Prince of Persia: The Sands of Time"),
    }
    games = _builder().build_games_from_configs(
        [base, sot], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {
        "Prince of Persia",
        "Prince of Persia: The Sands of Time",
    }


def test_colon_sequel_survives_with_catalog_when_itself_known():
    """Watch Dogs: Legion stays even when base Watch Dogs is owned.

    Legion is itself a catalog base game, so the colon rule's "full title
    not in catalog" clause keeps the standalone sequel.
    """
    base = _cfg(1, "", "Watch Dogs")
    legion = _cfg(2, "", "Watch Dogs: Legion")
    catalog = {_norm("Watch Dogs"), _norm("Watch Dogs: Legion")}
    games = _builder().build_games_from_configs(
        [base, legion], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"Watch Dogs", "Watch Dogs: Legion"}


def test_colon_dlc_kept_when_base_not_separately_owned():
    """The colon rule fires only when the base is separately owned.

    With just the DLC entry present (no standalone "Trials Fusion"), we
    keep it rather than drop an entry we can't prove is DLC.
    """
    dlc = _cfg(671, "", "Trials Fusion: Riders of the Rustlands")
    catalog = {_norm("Trials Fusion")}
    games = _builder().build_games_from_configs(
        [dlc], installed={}, base_catalog_norms=catalog,
    )
    assert _titles(games) == {"Trials Fusion: Riders of the Rustlands"}


@pytest.mark.parametrize(
    ("parent", "exact", "substr", "expected"),
    [
        ("far cry", {"far cry"}, set(), True),  # exact
        ("tom", {"tommy"}, {"tommy"}, False),  # too short for substring
        (
            "rainbow six siege",
            set(),
            {"tom clancys rainbow six siege"},
            True,
        ),  # substring fallback
        ("", {"far cry"}, {"far cry"}, False),  # empty parent
    ],
)
def test_parent_matches(parent, exact, substr, expected):
    assert _IdentityResolver._parent_matches(parent, exact, substr) is expected
