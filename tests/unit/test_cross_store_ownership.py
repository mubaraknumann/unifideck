"""Which non-Steam stores hold a Steam game: the ownership ribbon's join.

The ribbon tells a user "you already own this" on a page where they are about
to pay. A false positive costs a sale they wanted; a false negative costs the
whole feature. These tests pin the decisions that keep the join honest: both
AppID forms resolve, the ``-1`` sentinel and stale mappings never match, rows
that are not ownership (DLC, demos, betas, Steam itself) are skipped, and
subscription libraries are kept apart from purchases.
"""
from __future__ import annotations

from typing import Any

from unifideck.core.cross_store_ownership import OwnedCopy, find_owned_copies
from unifideck.core.steam_appid_map import STEAM_REAL_APPID_NS
from unifideck.core.types.domain import Game

BG2 = 257350
SIGNED = -1867837430
UNSIGNED = SIGNED & 0xFFFFFFFF


class _Cache:
    """The one read the join performs."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries

    def get(self, namespace: str, key: str) -> Any:
        assert namespace == STEAM_REAL_APPID_NS
        return self.entries.get(key)


class _RaisingCache:
    def get(self, namespace: str, key: str) -> Any:
        raise RuntimeError("cache unavailable")


def _game(app_id: int, store: str, title: str = "Baldur's Gate II", **kw: Any) -> Game:
    return Game(app_id=app_id, store=store, store_game_id=f"{store}-{app_id}", title=title, **kw)


def test_finds_a_copy_mapped_under_the_signed_form() -> None:
    games = [_game(SIGNED, "gog")]
    copies = find_owned_copies(games, _Cache({str(SIGNED): BG2}), BG2)

    assert copies == [OwnedCopy("gog", ("Baldur's Gate II",), False, False)]


def test_finds_a_copy_whose_app_id_is_unsigned() -> None:
    """``Game.app_id`` is signed today, but the read must not depend on it."""
    games = [_game(UNSIGNED, "gog")]

    assert [c.store for c in find_owned_copies(games, _Cache({str(SIGNED): BG2}), BG2)] == ["gog"]


def test_the_no_counterpart_sentinel_and_junk_never_match() -> None:
    games = [_game(1, "gog"), _game(2, "epic"), _game(3, "amazon")]
    cache = _Cache({"1": -1, "2": 0, "3": "257350"})

    assert find_owned_copies(games, cache, BG2) == []


def test_a_mapping_for_a_game_no_longer_in_the_library_is_ignored() -> None:
    """``steam_real_appid`` is never pruned; the library is the source."""
    cache = _Cache({"99": BG2, str(SIGNED): 1091500})
    games = [_game(SIGNED, "gog")]

    assert find_owned_copies(games, cache, BG2) == []


def test_two_copies_on_one_store_merge_into_one() -> None:
    games = [
        _game(1, "gog", "Baldur's Gate II"),
        _game(2, "gog", "Baldur's Gate II: Enhanced Edition", installed=True),
        _game(3, "gog", "Baldur's Gate II"),
    ]
    cache = _Cache({"1": BG2, "2": BG2, "3": BG2})

    assert find_owned_copies(games, cache, BG2) == [
        OwnedCopy(
            "gog",
            ("Baldur's Gate II", "Baldur's Gate II: Enhanced Edition"),
            installed=True,
            subscription=False,
        ),
    ]


def test_subscription_rows_are_marked_and_sort_after_purchases() -> None:
    games = [_game(1, "microsoft"), _game(2, "gog"), _game(3, "amazon", installed=True)]
    cache = _Cache({"1": BG2, "2": BG2, "3": BG2})

    copies = find_owned_copies(games, cache, BG2)

    assert [(c.store, c.subscription) for c in copies] == [
        ("amazon", False),  # installed first
        ("gog", False),
        ("microsoft", True),
    ]


def test_non_ownership_rows_are_skipped() -> None:
    """A DLC fuzzy-matched to its base game must not claim the base game."""
    games = [
        _game(1, "gog", tags=["dlc"]),
        _game(2, "epic", tags=["demo"]),
        _game(3, "amazon", tags=["beta"]),
        _game(4, "steam"),
        _game(5, "itch", tags=["native"]),
    ]
    cache = _Cache({str(i): BG2 for i in range(1, 6)})

    assert [c.store for c in find_owned_copies(games, cache, BG2)] == ["itch"]


def test_a_raising_or_missing_cache_yields_nothing() -> None:
    games = [_game(1, "gog")]

    assert find_owned_copies(games, _RaisingCache(), BG2) == []
    assert find_owned_copies(games, None, BG2) == []


def test_a_non_positive_app_id_short_circuits() -> None:
    games = [_game(1, "gog")]

    assert find_owned_copies(games, _Cache({"1": BG2}), 0) == []
    assert find_owned_copies(games, _Cache({"1": BG2}), -5) == []



# ── Other versions of the game (shared with the library's grouping) ──

def test_an_owned_remaster_shows_on_the_original_s_page() -> None:
    """Mass Effect (2007) page: the Legendary Edition on Xbox counts."""
    from unifideck.core.game_identity import SteamApp

    games = [_game(7, "microsoft", "Mass Effect™ Legendary Edition")]
    copies = find_owned_copies(
        games, _Cache({"7": 1328670}), 17460,
        owned_steam=[SteamApp(17460, "Mass Effect (2007)")],
    )

    assert [(c.store, c.titles) for c in copies] == [
        ("microsoft", ("Mass Effect™ Legendary Edition",)),
    ]


def test_an_unowned_page_finds_versions_by_its_steam_name() -> None:
    games = [_game(8, "gog", "Dishonored - Definitive Edition")]
    copies = find_owned_copies(games, _Cache({}), 205100, steam_name="Dishonored")

    assert [c.store for c in copies] == ["gog"]


def test_a_same_named_different_game_does_not_count() -> None:
    """Battlefront II (2017) on Xbox is not the 2005 game on this page."""
    games = [_game(9, "microsoft", "STAR WARS™ Battlefront™ II")]
    copies = find_owned_copies(
        games, _Cache({"9": 1237950}), 6060,
        steam_name="Star Wars: Battlefront 2 (Classic, 2005)",
    )

    assert copies == []


def test_an_identical_owned_title_moves_a_wrong_mapping() -> None:
    """Xbox "Thief" was mapped to Thief Gold; it is the owned "Thief"."""
    from unifideck.core.game_identity import SteamApp

    games = [_game(10, "microsoft", "Thief")]
    owned = [SteamApp(239160, "Thief"), SteamApp(211600, "Thief Gold")]
    cache = _Cache({"10": 211600})

    assert find_owned_copies(games, cache, 211600, owned_steam=owned) == []
    assert [c.store for c in find_owned_copies(games, cache, 239160, owned_steam=owned)] == [
        "microsoft",
    ]


def test_a_presumed_battlenet_row_is_not_ownership() -> None:
    """Battle.net presumes a game account for every free-to-play program.

    That earns a library tile, but the ribbon sits above the purchase
    options: "Already owned on Battle.net" must not rest on a guess.
    """
    presumed = _game(SIGNED, "battlenet", metadata={"ownership": "presumed"})
    granted = _game(UNSIGNED + 1, "battlenet", metadata={"ownership": "granted"})
    cache = _Cache({str(SIGNED): BG2, str((UNSIGNED + 1) - 0x100000000): BG2})

    assert find_owned_copies([presumed], cache, BG2) == []
    assert [c.store for c in find_owned_copies([presumed, granted], cache, BG2)] == ["battlenet"]
