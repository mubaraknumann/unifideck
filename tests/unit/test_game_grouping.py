"""Tests for the display-only cross-store duplicate grouping.

Exercises ``annotate_duplicate_groups`` (and through it
``core.game_identity``) against the duplicate examples surfaced by users
(Behind the Frame, Bus Simulator 21, Dungeon of Naheulbeuk, Doors), the
version rule (remasters and editions group, sequels, years and
same-named different games do not), and the owned-Steam cross-reference.
Real titles and AppIDs come from the 2026-10-03 full-library POC.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.core import game_grouping
from unifideck.core.game_grouping import (
    annotate_duplicate_groups,
    annotate_duplicate_groups_if_enabled,
)
from unifideck.core.game_identity import SteamApp
from unifideck.core.types import Game


def _g(store: str, title: str, app_id: int = 0) -> Game:
    return Game(
        app_id=app_id,
        store=store,
        store_game_id=f"{store}-{title}".lower().replace(" ", "-"),
        title=title,
    )


def _mapped(games: list[Game], appids: dict[str, int]) -> list[Game]:
    """Annotate with ``title -> real Steam AppID`` as the mapping."""
    return annotate_duplicate_groups(
        games, steam_appid_of=lambda g: appids.get(g.title),
    )


def _grouped(a: Game, b: Game) -> bool:
    return a.dedupe_group_id is not None and a.dedupe_group_id == b.dedupe_group_id


def test_unique_titles_are_not_grouped():
    games = [_g("epic", "Game A"), _g("gog", "Game B")]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id is None
    assert out[1].dedupe_group_id is None


def test_behind_the_frame_colon_matches_across_stores():
    games = [
        _g("epic", "Behind the Frame: The Finest Scenery"),
        _g("amazon", "Behind the Frame: The Finest Scenery"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id is not None
    assert out[0].dedupe_group_id == out[1].dedupe_group_id


def test_bus_simulator_colon_vs_no_colon():
    games = [
        _g("epic", "Bus Simulator 21 Next Stop"),
        _g("amazon", "Bus Simulator 21: Next Stop"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id == out[1].dedupe_group_id


def test_dungeon_of_naheulbeuk_casing_and_word_order_of_articles():
    games = [
        _g("amazon", "The Dungeon Of Naheulbeuk: The Amulet Of Chaos"),
        _g("epic", "The Dungeon of Naheulbeuk: The Amulet of Chaos"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id == out[1].dedupe_group_id


def test_doors_colon_vs_hyphen():
    games = [
        _g("amazon", "Doors: Paradox"),
        _g("epic", "Doors - Paradox"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id == out[1].dedupe_group_id


def test_sequel_never_grouped_with_base_game():
    games = [_g("epic", "Beholder"), _g("amazon", "Beholder 2")]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id is None
    assert out[1].dedupe_group_id is None


def test_edition_variant_groups_and_carries_edition_label():
    games = [
        _g("epic", "Cyberpunk 2077"),
        _g("gog", "Cyberpunk 2077: Ultimate Edition"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id == out[1].dedupe_group_id
    assert out[0].edition_label is None
    assert out[1].edition_label == "Ultimate Edition"


def test_publisher_prefix_variant_still_groups():
    games = [
        _g("epic", "Splinter Cell Chaos Theory"),
        _g("ubisoft", "Tom Clancy's Splinter Cell Chaos Theory"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id is not None
    assert out[0].dedupe_group_id == out[1].dedupe_group_id


def test_grouping_never_changes_count_or_order():
    games = [
        _g("epic", "Behind the Frame: The Finest Scenery"),
        _g("gog", "Unrelated Game"),
        _g("amazon", "Behind the Frame: The Finest Scenery"),
    ]
    out = annotate_duplicate_groups(games)
    assert len(out) == 3
    assert [g.store for g in out] == ["epic", "gog", "amazon"]


# ── Versions of one game group together ─────────────────────────────


@pytest.mark.parametrize(("title_a", "title_b"), [
    ("Mass Effect™ Legendary Edition", "Mass Effect"),
    ("The Elder Scrolls IV: Oblivion Remastered",
     "The Elder Scrolls IV: Oblivion Game of the Year Edition"),
    ("Dishonored - Definitive Edition", "Dishonored"),
    ("Disco Elysium - The Final Cut", "Disco Elysium"),
    ("Gears of War: Reloaded", "Gears of War: Ultimate Edition"),
    ("Saints Row IV: Re-Elected", "Saints Row IV"),
    ("Metro Last Light Redux", "Metro: Last Light"),
    ("Quake II (Original)", "Quake II"),
    ("Mafia: Definitive Edition for XBOX One", "Mafia: Definitive Edition"),
    ("Medieval Dynasty (Xbox One)", "Medieval Dynasty"),
    ("Thief II", "Thief 2"),
])
def test_versions_of_one_game_group(title_a: str, title_b: str) -> None:
    a, b = annotate_duplicate_groups([_g("epic", title_a), _g("gog", title_b)])
    assert _grouped(a, b)


def test_an_edition_qualifier_folds_onto_the_base_game() -> None:
    """"<words> Edition" loses its qualifier when the base game exists."""
    games = annotate_duplicate_groups([
        _g("epic", "The Outer Worlds"),
        _g("gog", "The Outer Worlds: Spacer's Choice Edition"),
        _g("microsoft", "Hollow Knight: Voidheart Edition"),
        _g("gog", "Hollow Knight"),
    ])
    assert _grouped(games[0], games[1])
    assert _grouped(games[2], games[3])


# ── Different games stay apart ──────────────────────────────────────


@pytest.mark.parametrize(("title_a", "title_b"), [
    ("Car Mechanic Simulator 2021", "Car Mechanic Simulator 2018"),
    ("Microsoft Flight Simulator (2020)", "Microsoft Flight Simulator 2024"),
    ("EA SPORTS FC™ 25 Xbox One", "EA SPORTS FC™ 26 Xbox One"),
    ("Far Cry 3 Blood Dragon", "Far Cry® 3"),
    ("Tomb Raider: Anniversary", "Tomb Raider"),
    ("BioShock Infinite", "BioShock"),
    ("Fallout", "Fallout: New Vegas"),
    ("Thief", "Thief II"),
    ("Cyber Revolution", "Cyber"),
    ("Creative Console", "Creative"),
    ("Borderlands 2 DLC", "Borderlands 2"),
])
def test_different_games_stay_apart(title_a: str, title_b: str) -> None:
    a, b = annotate_duplicate_groups([_g("epic", title_a), _g("gog", title_b)])
    assert not _grouped(a, b)


@pytest.mark.parametrize(("title_a", "appid_a", "title_b", "appid_b"), [
    ("STAR WARS™ Battlefront™ II", 1237950,
     "Star Wars: Battlefront 2 (Classic, 2005)", 6060),
    ("Dead Space", 1693980, "Dead Space (2008)", 17470),
])
def test_same_name_on_different_steam_apps_stays_apart(
    title_a: str, appid_a: int, title_b: str, appid_b: int,
) -> None:
    """No version word explains the different Steam app: two games."""
    a, b = _mapped(
        [_g("microsoft", title_a), _g("gog", title_b)],
        {title_a: appid_a, title_b: appid_b},
    )
    assert not _grouped(a, b)


def test_a_version_word_explains_a_different_steam_app() -> None:
    a, b = _mapped(
        [_g("microsoft", "Mass Effect™ Legendary Edition"), _g("gog", "Mass Effect (2007)")],
        {"Mass Effect™ Legendary Edition": 1328670, "Mass Effect (2007)": 17460},
    )
    assert _grouped(a, b)


def test_a_shared_steam_app_joins_different_titles() -> None:
    a, b = _mapped(
        [_g("epic", "Orwell: Keeping an Eye on You"), _g("gog", "Orwell")],
        {"Orwell: Keeping an Eye on You": 491950, "Orwell": 491950},
    )
    assert _grouped(a, b)


def test_bioshock_versions_group_without_chaining_into_infinite() -> None:
    games = annotate_duplicate_groups([
        _g("gog", "BioShock™"),
        _g("gog", "BioShock™ Remastered"),
        _g("gog", "BioShock Infinite Complete Edition"),
    ])
    assert _grouped(games[0], games[1])
    assert not _grouped(games[0], games[2])
    assert games[2].dedupe_group_id is None


def test_group_id_is_stable_regardless_of_input_order() -> None:
    titles = ["BioShock™", "BioShock™ Remastered", "BioShock Infinite", "BioShock 2"]
    forward = annotate_duplicate_groups([_g("gog", t) for t in titles])
    backward = annotate_duplicate_groups([_g("gog", t) for t in reversed(titles)])
    ids = {g.title: g.dedupe_group_id for g in forward}
    assert ids == {g.title: g.dedupe_group_id for g in backward}


# ── Owned Steam games ───────────────────────────────────────────────


def test_steam_owned_match_sets_app_id_and_steam_edition_label() -> None:
    (game,) = annotate_duplicate_groups(
        [_g("epic", "Disco Elysium")],
        steam_owned=[SteamApp(632470, "Disco Elysium - The Final Cut")],
    )
    assert game.steam_owned_app_id == 632470
    assert game.steam_owned_edition_label == "The Final Cut"
    assert game.dedupe_group_id is None  # one library copy is not a group


def test_steam_owned_respects_the_sequel_boundary() -> None:
    (game,) = annotate_duplicate_groups(
        [_g("epic", "Beholder 2")], steam_owned=[SteamApp(475550, "Beholder")],
    )
    assert game.steam_owned_app_id is None


def test_steam_owned_apostrophes_match() -> None:
    (game,) = annotate_duplicate_groups(
        [_g("microsoft", "Assassin’s Creed® Odyssey")],  # noqa: RUF001 — the curly apostrophe is the case under test
        steam_owned=[SteamApp(812140, "Assassin's Creed Odyssey")],
    )
    assert game.steam_owned_app_id == 812140


def test_steam_owned_remaster_matches_the_original() -> None:
    (game,) = annotate_duplicate_groups(
        [_g("microsoft", "The Elder Scrolls IV: Oblivion Remastered")],
        steam_owned=[SteamApp(22330, "The Elder Scrolls IV: Oblivion Game of the Year Edition (2009)")],
    )
    assert game.steam_owned_app_id == 22330


def test_each_copy_points_at_its_own_owned_version() -> None:
    """Owning BioShock 2 and its Remastered on Steam: each row its own."""
    games = annotate_duplicate_groups(
        [_g("gog", "BioShock® 2"), _g("gog", "BioShock™ 2 Remastered")],
        steam_owned=[SteamApp(8850, "BioShock 2"), SteamApp(409720, "BioShock 2 Remastered")],
    )
    assert [g.steam_owned_app_id for g in games] == [8850, 409720]


def test_an_identical_title_beats_a_wrong_mapping() -> None:
    """Xbox "Thief" (2014) was mapped to Thief Gold by the store search."""
    (game,) = _mapped_with_owned(
        [_g("microsoft", "Thief")], {"Thief": 211600},
        [SteamApp(239160, "Thief"), SteamApp(211600, "Thief Gold")],
    )
    assert game.steam_owned_app_id == 239160


def test_a_mapped_unowned_remake_never_takes_the_owned_original() -> None:
    (game,) = _mapped_with_owned(
        [_g("microsoft", "Dead Space")], {"Dead Space": 1693980},
        [SteamApp(17470, "Dead Space (2008)")],
    )
    assert game.steam_owned_app_id is None


def _mapped_with_owned(
    games: list[Game], appids: dict[str, int], owned: list[SteamApp],
) -> list[Game]:
    return annotate_duplicate_groups(
        games, steam_appid_of=lambda g: appids.get(g.title), steam_owned=owned,
    )


# ── Re-annotation and the setting ───────────────────────────────────


def test_a_match_that_no_longer_holds_is_cleared() -> None:
    """Fields round-trip through library_cache.json, so a stale match
    from an earlier run must not survive the next annotation."""
    game = _g("microsoft", "Mass Effect™ Legendary Edition")
    game.steam_owned_app_id = 17460
    game.steam_owned_edition_label = "stale"
    game.dedupe_group_id = "stale"
    annotate_duplicate_groups([game])
    assert game.steam_owned_app_id is None
    assert game.steam_owned_edition_label is None
    assert game.dedupe_group_id is None


class _Config:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def get(self, key: str, default: Any) -> Any:
        return self._enabled if key == "dedup.ui_grouping_enabled" else default


def test_turning_grouping_off_clears_every_field() -> None:
    games = annotate_duplicate_groups(
        [_g("epic", "Dishonored"), _g("gog", "Dishonored - Definitive Edition")],
        steam_owned=[SteamApp(205100, "Dishonored")],
    )
    assert games[0].dedupe_group_id is not None

    annotate_duplicate_groups_if_enabled(games, _Config(enabled=False))

    for game in games:
        assert game.dedupe_group_id is None
        assert game.edition_label is None
        assert game.steam_owned_app_id is None
        assert game.steam_owned_edition_label is None


def test_enabled_path_reads_the_mapping_cache_and_owned_library(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        game_grouping, "load_owned_steam_apps",
        lambda _config: [SteamApp(17470, "Dead Space (2008)")],
    )

    class _Cache:
        def get(self, namespace: str, key: str) -> Any:
            return {"-5": 1693980}.get(key) if namespace == "steam_real_appid" else None

    (game,) = annotate_duplicate_groups_if_enabled(
        [_g("microsoft", "Dead Space", app_id=-5)], _Config(enabled=True), _Cache(),
    )
    assert game.steam_owned_app_id is None


def test_every_owned_steam_version_is_listed_on_every_copy() -> None:
    """BioShock and BioShock Remastered owned on Steam and on GOG: each GOG
    copy points at its own Steam version, and both list both."""
    games = annotate_duplicate_groups(
        [_g("gog", "BioShock™"), _g("gog", "BioShock™ Remastered")],
        steam_owned=[SteamApp(7670, "BioShock"), SteamApp(409710, "BioShock Remastered")],
    )
    expected = [
        {"app_id": 7670, "edition_label": None},
        {"app_id": 409710, "edition_label": "Remastered"},
    ]
    assert [g.steam_owned_app_id for g in games] == [7670, 409710]
    assert [g.steam_versions for g in games] == [expected, expected]


def test_steam_versions_are_cleared_with_the_other_fields() -> None:
    game = _g("gog", "Unrelated Game")
    game.steam_versions = [{"app_id": 1, "edition_label": None}]
    annotate_duplicate_groups([game])
    assert game.steam_versions == []
