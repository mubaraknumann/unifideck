"""Tests for the display-only cross-store duplicate grouping.

Exercises ``annotate_duplicate_groups`` against the exact duplicate
examples surfaced by the user (Behind the Frame, Bus Simulator 21,
Dungeon of Naheulbeuk, Doors) plus the negative case that must never
group (sequels).
"""
from __future__ import annotations

import pytest

from unifideck.core.game_grouping import annotate_duplicate_groups
from unifideck.core.types import Game
from unifideck.steam.owned_games import OwnedApp


def _g(store: str, title: str) -> Game:
    return Game(
        app_id=0,
        store=store,
        store_game_id=f"{store}-{title}".lower().replace(" ", "-"),
        title=title,
    )


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


def test_steam_owned_match_sets_app_id():
    games = [_g("epic", "Disco Elysium")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={"disco elysium": OwnedApp(appid=632470, title="Disco Elysium")},
    )
    assert out[0].steam_owned_app_id == 632470


def test_steam_owned_no_match_leaves_field_none():
    games = [_g("epic", "Disco Elysium")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={"some other game": OwnedApp(appid=12345, title="Some Other Game")},
    )
    assert out[0].steam_owned_app_id is None


def test_steam_owned_is_independent_of_dedupe_group():
    """A singleton title (no cross-store dupe) can still carry
    steam_owned_app_id — the two fields answer different questions."""
    games = [_g("epic", "Disco Elysium")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={"disco elysium": OwnedApp(appid=632470, title="Disco Elysium")},
    )
    assert out[0].dedupe_group_id is None
    assert out[0].steam_owned_app_id == 632470


def test_steam_owned_respects_sequel_boundary():
    """Beholder 2 must not match a Steam-owned "Beholder"."""
    games = [_g("amazon", "Beholder 2")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={"beholder": OwnedApp(appid=705810, title="Beholder")},
    )
    assert out[0].steam_owned_app_id is None


def test_steam_owned_none_is_a_noop():
    games = [_g("epic", "Disco Elysium")]
    out = annotate_duplicate_groups(games, steam_owned=None)
    assert out[0].steam_owned_app_id is None


def test_steam_owned_edition_label_extracted_from_steam_title():
    """Disco Elysium's real regression: the Epic copy and the Steam
    listing are BOTH "The Final Cut" — the switcher must show that, not
    a blank/default label, for the synthetic Steam entry."""
    games = [_g("epic", "Disco Elysium")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={
            "disco elysium": OwnedApp(
                appid=632470, title="Disco Elysium - The Final Cut",
            ),
        },
    )
    assert out[0].steam_owned_edition_label == "The Final Cut"


def test_steam_owned_edition_label_none_when_steam_title_has_no_suffix():
    games = [_g("epic", "Disco Elysium")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={"disco elysium": OwnedApp(appid=632470, title="Disco Elysium")},
    )
    assert out[0].steam_owned_edition_label is None


def test_steam_owned_edition_label_independent_of_this_games_own_edition():
    """The Steam copy's edition label must come from the Steam title,
    not get contaminated by this game's own (possibly different) one."""
    games = [_g("epic", "Disco Elysium - Definitive Edition")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={
            "disco elysium definitive edition": OwnedApp(
                appid=632470, title="Disco Elysium - The Final Cut",
            ),
        },
    )
    assert out[0].edition_label == "Definitive Edition"
    assert out[0].steam_owned_edition_label == "The Final Cut"


# ── PR #461 review fixes ────────────────────────────────────────────
# https://github.com/mubaraknumann/unifideck/pull/461#pullrequestreview-5307127830


def test_steam_owned_match_survives_apostrophe_normalizer_mismatch():
    """A.1 — steam_owned's dict keys come from unifidb's normaliser
    (strips apostrophes: "assassins creed"), while everything else here
    uses title_match's (keeps a space: "assassin s"). Matching against
    the dict KEY silently dropped every apostrophe'd title's Steam
    cross-reference; matching against OwnedApp.title (the original)
    fixes it."""
    games = [_g("epic", "Assassin's Creed")]
    out = annotate_duplicate_groups(
        games,
        steam_owned={
            "assassins creed": OwnedApp(appid=48190, title="Assassin's Creed"),
        },
    )
    assert out[0].steam_owned_app_id == 48190


def test_steam_owned_picks_exact_match_over_shorter_bucket_neighbour():
    """A.2 — "BioShock Infinite" must match the owned "BioShock
    Infinite" (8870), not whichever of the two bucket entries dict
    iteration visits first (previously could return "BioShock", 7670)."""
    games = [_g("epic", "BioShock Infinite")]
    steam_owned = {
        "bioshock": OwnedApp(appid=7670, title="BioShock"),
        "bioshock infinite": OwnedApp(appid=8870, title="BioShock Infinite"),
    }
    out = annotate_duplicate_groups(games, steam_owned=steam_owned)
    assert out[0].steam_owned_app_id == 8870


def test_steam_owned_picks_thief_gold_not_base_thief():
    """A.2 — "Thief Gold" must match owned "Thief Gold" (211600), not
    the unrelated "Thief" (2014) also sharing the "thief" bucket."""
    games = [_g("epic", "Thief Gold")]
    steam_owned = {
        "thief 2014": OwnedApp(appid=239160, title="Thief"),
        "thief gold": OwnedApp(appid=211600, title="Thief Gold"),
    }
    out = annotate_duplicate_groups(games, steam_owned=steam_owned)
    assert out[0].steam_owned_app_id == 211600


def test_steam_owned_picks_skyrim_special_edition_not_base_skyrim():
    """A.2 — "Skyrim Special Edition" must match the owned SE (489830),
    not the base "Skyrim" (72850) sharing its bucket."""
    games = [_g("epic", "The Elder Scrolls V: Skyrim Special Edition")]
    steam_owned = {
        "the elder scrolls v skyrim": OwnedApp(
            appid=72850, title="The Elder Scrolls V: Skyrim",
        ),
        "the elder scrolls v skyrim special edition": OwnedApp(
            appid=489830, title="The Elder Scrolls V: Skyrim Special Edition",
        ),
    }
    out = annotate_duplicate_groups(games, steam_owned=steam_owned)
    assert out[0].steam_owned_app_id == 489830


@pytest.mark.parametrize(
    ("title_a", "title_b"),
    [
        ("Fallout", "Fallout: New Vegas"),
        ("Microsoft Flight Simulator 2020", "Microsoft Flight Simulator 2024"),
        ("Dead Space", "Dead Space (2023)"),
        ("Killer Instinct", "Killer Instinct Classic"),
        ("Mass Effect Legendary Edition", "Mass Effect"),
        (
            "The Elder Scrolls IV: Oblivion Remastered",
            "The Elder Scrolls IV: Oblivion Game of the Year Edition",
        ),
        ("Tomb Raider: Anniversary", "Tomb Raider (2013)"),
        ("Far Cry 3: Blood Dragon", "Far Cry 3"),
        ("Car Mechanic Simulator 2021", "Car Mechanic Simulator 2018"),
        ("Star Wars Battlefront II (2017)", "Star Wars Battlefront 2 (2005)"),
        # Found via a live-library audit: "Definitive Edition" is used
        # both for a distinct remaster with its own store page
        # (Dishonored, Thief, Tomb Raider, Ori and the Blind Forest all
        # have one) and for a publisher's only current listing of an
        # older game (Mafia, Gamedec — no separate unsuffixed release
        # exists, so those stay correctly grouped; see
        # test_definitive_edition_reissue_still_groups_with_itself
        # below). Grouping must assume the first case, same as
        # "Remastered"/"Remake".
        ("Dishonored", "Dishonored - Definitive Edition"),
        ("Thief", "THIEF: Definitive Edition"),
        ("Tomb Raider", "Tomb Raider: Definitive Edition"),
        ("Ori and the Blind Forest", "Ori and the Blind Forest: Definitive Edition"),
    ],
)
def test_sequels_remakes_and_years_are_never_grouped(title_a, title_b):
    """A.3 — sequels, remakes/remasters, and differing yearly releases
    must never share a card; `titles_match`'s fuzzy tolerance (built for
    artwork lookup) is deliberately NOT reused here.

    Each title also gets a same-store "twin" (identical title, a
    different store) so a real group forms on both sides — proving
    ``title_a`` and ``title_b`` don't merge into ONE group rather than
    merely observing two singletons, which would trivially satisfy a
    weaker "not equal" check since singletons are always ``None``."""
    games = [
        _g("epic", title_a),
        _g("gog", title_a),
        _g("amazon", title_b),
        _g("ubisoft", title_b),
    ]
    out = annotate_duplicate_groups(games)
    group_a = {out[0].dedupe_group_id, out[1].dedupe_group_id}
    group_b = {out[2].dedupe_group_id, out[3].dedupe_group_id}
    assert None not in group_a
    assert None not in group_b
    assert group_a.isdisjoint(group_b)


def test_definitive_edition_reissue_still_groups_with_itself():
    """Refusing to strip "Definitive Edition" for grouping (see the
    parametrized case above) must not stop two store copies of the SAME
    Definitive Edition release from grouping with each other — only
    cross-store copies of an unsuffixed sibling should be kept apart.
    Mirrors "Mafia: Definitive Edition" / "Gamedec - Definitive Edition"
    from the live-library audit, neither of which has a separate
    unsuffixed release in the library to conflict with."""
    games = [
        _g("epic", "Mafia: Definitive Edition"),
        _g("gog", "Mafia: Definitive Edition"),
    ]
    out = annotate_duplicate_groups(games)
    assert out[0].dedupe_group_id is not None
    assert out[0].dedupe_group_id == out[1].dedupe_group_id


def test_bioshock_chain_never_transitively_groups():
    """A.4 — union-find used to chain BioShock ~ BioShock Infinite and
    BioShock ~ BioShock Remastered into one group even though Infinite
    and Remastered don't match each other. Canonical-key grouping can't
    chain: none of the three share an exact canonical key, so none
    group at all (each stays its own singleton, dedupe_group_id=None)."""
    games = [
        _g("epic", "BioShock"),
        _g("gog", "BioShock Infinite"),
        _g("amazon", "BioShock Remastered"),
    ]
    out = annotate_duplicate_groups(games)
    non_singleton_ids = [g.dedupe_group_id for g in out if g.dedupe_group_id]
    assert len(non_singleton_ids) == len(set(non_singleton_ids))


def test_group_id_is_stable_regardless_of_input_order():
    """A.5 — the group id is the canonical key itself, not a union-find
    root, so it doesn't depend on which order the games were passed
    in."""
    forward = [
        _g("epic", "Cyberpunk 2077"),
        _g("gog", "Cyberpunk 2077: Ultimate Edition"),
    ]
    reversed_ = [
        _g("gog", "Cyberpunk 2077: Ultimate Edition"),
        _g("epic", "Cyberpunk 2077"),
    ]
    out_forward = annotate_duplicate_groups(forward)
    out_reversed = annotate_duplicate_groups(reversed_)
    assert (
        sorted(g.dedupe_group_id for g in out_forward)
        == sorted(g.dedupe_group_id for g in out_reversed)
    )
