"""The ribbon join with an authenticated purchase index (Xbox).

Without an index every Microsoft row is "playable via Xbox Cloud Gaming",
never "owned", because the xCloud list mixes Game Pass with purchases. With
one, measured against the user's console on 2026-10-02:

- a library row whose product is in the index is OWNED; one that is not is
  still CLOUD;
- streaming is two facts (measured 2026-10-04): an owned title outside Game
  Pass streams as the user's own ("Cloud" on the owned copy); a Game Pass
  title is a separate Game Pass copy, owned or not; a row whose Game Pass
  flag is unknown, or that is neither owned nor in Game Pass, is a neutral
  cloud copy;
- an owned product with no library row (it cannot stream) is found through
  the index's own Steam AppID;
- a product with a library row answers through that row only, never twice;
- the copy says where it plays and whether it needs a subscription (Gold).
"""
from __future__ import annotations

from typing import Any

from unifideck.core.cross_store_ownership import (
    GAME_PASS_KEY,
    OwnedCopy,
    PurchasedProduct,
    PurchaseIndex,
    find_owned_copies,
    platform_label,
)
from unifideck.core.steam_appid_map import STEAM_REAL_APPID_NS
from unifideck.core.types.domain import Game

SEKIRO, FH4, ATOMFALL = 814380, 1293830, 801800


class _Cache:
    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries

    def get(self, namespace: str, key: str) -> Any:
        assert namespace == STEAM_REAL_APPID_NS
        return self.entries.get(key)


def _xcloud(app_id: int, pid: str, title: str, game_pass: bool | None = None) -> Game:
    metadata = {} if game_pass is None else {GAME_PASS_KEY: game_pass}
    return Game(app_id=app_id, store="microsoft", store_game_id=pid, title=title,
                tags=["xcloud", "browser"], metadata=metadata)


def _index(products: dict[str, PurchasedProduct], by_appid: dict[int, tuple[str, ...]]) -> dict[str, PurchaseIndex]:
    return {"microsoft": PurchaseIndex(products=products, by_steam_appid=by_appid)}


FH4_ROW = _xcloud(-11, "9pnqkhfld2wq", "Forza Horizon 4", game_pass=False)  # lowercase id: case-folded
ATOMFALL_ROW = _xcloud(-12, "9NC4WDL5KZBL", "Atomfall", game_pass=True)
CACHE = _Cache({"-11": FH4, "-12": ATOMFALL})
OWNED = _index(
    {
        "9PNQKHFLD2WQ": PurchasedProduct("Forza Horizon 4", pc=True, console=True, play_anywhere=True),
        "BQD5WRRP2D6Q": PurchasedProduct("Sekiro: Shadows Die Twice - GOTY Edition", console=True),
    },
    {FH4: ("9PNQKHFLD2WQ",), SEKIRO: ("BQD5WRRP2D6Q",)},
)


def test_an_owned_title_outside_game_pass_streams_as_owned() -> None:
    [copy] = find_owned_copies([FH4_ROW], CACHE, FH4, OWNED)
    assert copy == OwnedCopy(
        "microsoft", ("Forza Horizon 4",), False, False,
        streams=True, platform="play_anywhere", gold=False,
    )


def test_an_owned_game_pass_title_is_owned_and_a_separate_game_pass_copy() -> None:
    row = _xcloud(-11, "9PNQKHFLD2WQ", "Forza Horizon 4", game_pass=True)
    owned, game_pass = find_owned_copies([row], CACHE, FH4, OWNED)
    assert (owned.subscription, owned.streams, owned.platform) == (False, False, "play_anywhere")
    assert (game_pass.subscription, game_pass.game_pass) == (True, True)


def test_an_owned_title_with_an_unknown_flag_streams_neutrally() -> None:
    """Rows synced before the catalog flag existed: we cannot say why it streams."""
    row = _xcloud(-11, "9PNQKHFLD2WQ", "Forza Horizon 4")
    owned, cloud = find_owned_copies([row], CACHE, FH4, OWNED)
    assert owned.streams is False
    assert (cloud.subscription, cloud.game_pass) == (True, False)


def test_a_game_pass_row_stays_cloud() -> None:
    [copy] = find_owned_copies([ATOMFALL_ROW], CACHE, ATOMFALL, OWNED)
    assert copy.subscription is True
    assert copy.game_pass is True
    assert copy.platform == ""


def test_a_row_neither_owned_nor_in_game_pass_is_neutral() -> None:
    row = _xcloud(-12, "C2Z9D406G0KQ", "Atomfall", game_pass=False)
    [copy] = find_owned_copies([row], CACHE, ATOMFALL, OWNED)
    assert (copy.subscription, copy.game_pass) == (True, False)


def test_an_owned_title_that_cannot_stream_is_found_by_its_own_appid() -> None:
    [copy] = find_owned_copies([FH4_ROW, ATOMFALL_ROW], CACHE, SEKIRO, OWNED)
    assert copy.subscription is False
    assert copy.platform == "console"
    assert copy.streams is False
    assert copy.titles == ("Sekiro: Shadows Die Twice - GOTY Edition",)


def test_a_product_with_a_row_answers_through_the_row_only() -> None:
    """The index maps FH4's product to Sekiro by mistake; the row wins."""
    wrong = _index(dict(OWNED["microsoft"].products), {SEKIRO: ("9PNQKHFLD2WQ",)})
    assert find_owned_copies([FH4_ROW], CACHE, SEKIRO, wrong) == []


def test_without_an_index_microsoft_rows_are_cloud_as_before() -> None:
    [copy] = find_owned_copies([FH4_ROW], CACHE, FH4)
    assert copy.subscription is True
    assert copy.game_pass is False
    [copy] = find_owned_copies([ATOMFALL_ROW], CACHE, ATOMFALL)
    assert copy.game_pass is True


def test_gold_needs_every_owned_product_to_be_gold() -> None:
    gold = _index({"P1": PurchasedProduct("Darkwood", console=True, gold=True)}, {7: ("P1",)})
    [copy] = find_owned_copies([], CACHE, 7, gold)
    assert copy.gold is True and copy.platform == "console"


def test_other_stores_are_unaffected() -> None:
    gog = Game(app_id=-13, store="gog", store_game_id="1", title="Forza Horizon 4")
    stores = [c.store for c in find_owned_copies([gog, FH4_ROW], _Cache({"-13": FH4, "-11": FH4}), FH4, OWNED)]
    assert sorted(stores) == ["gog", "microsoft"]


def test_platform_labels() -> None:
    pc = PurchasedProduct("x", pc=True)
    console = PurchasedProduct("x", console=True)
    assert platform_label([pc]) == "pc"
    assert platform_label([console]) == "console"
    assert platform_label([pc, console]) == "pc_console"
    assert platform_label([PurchasedProduct("x", pc=True, console=True)]) == "play_anywhere"
    assert platform_label([PurchasedProduct("x", play_anywhere=True)]) == "play_anywhere"
    assert platform_label([PurchasedProduct("x")]) == ""
