"""Tests for ``stores/microsoft/pc_library/rows``: classify, merge, sticky ids,
the installed floor and the install overlay."""
from __future__ import annotations

from typing import Any

from unifideck.core.cross_store_ownership import GAME_PASS_KEY
from unifideck.core.types import Game, GameTag
from unifideck.stores.microsoft.pc_library.package_plan import PlanProducts
from unifideck.stores.microsoft.pc_library.rows import (
    PcTitle,
    StickyIds,
    dedupe_titles,
    installed_floor,
    installed_overlay,
    merge_with_xcloud,
    summarize_roots,
)

CID = "11111111-1111-4111-8111-111111111111"


def _xcloud(pid: str, title: str = "Game") -> Game:
    return Game(app_id=0, store="microsoft", store_game_id=pid, title=title,
                tags=[GameTag.XCLOUD, GameTag.BROWSER],
                metadata={"browser_url": f"https://www.xbox.com/play/launch/{pid}"})


def _title(ent: str, target: str | None = None, **over: Any) -> PcTitle:
    base: dict[str, Any] = {"entitlement_id": ent, "target_id": target or ent, "title": "Game",
                            "state": "ready", "reason": None, "access": "gamepass",
                            "content_id": CID}
    base.update(over)
    return PcTitle(**base)


def test_merge_on_exact_id_keeps_the_xcloud_key_and_tags() -> None:
    rows = merge_with_xcloud([_xcloud("9pk087lngjc5")], [_title("9PK087LNGJC5")], StickyIds(), "IN")
    assert len(rows) == 1
    row = rows[0]
    assert row.store_game_id == "9pk087lngjc5"
    assert row.tags == [GameTag.XCLOUD, GameTag.BROWSER, GameTag.XBOX_PC]
    assert row.metadata["browser_url"].endswith("9pk087lngjc5")
    assert row.metadata["xbox_pc"]["state"] == "ready"
    assert row.metadata["xbox_pc"]["market"] == "IN"
    assert row.metadata[GAME_PASS_KEY] is True


def test_the_same_game_under_two_ids_is_one_row_on_the_xcloud_tile() -> None:
    """PC Game Pass often lists its own PC product: one Xbox game, not two."""
    rows = merge_with_xcloud([_xcloud("9NBLGGH4PNC7", "Wolfenstein: The New Order")],
                             [_title("9AAAAAAAAAAA", title="Wolfenstein: The New Order (PC)")],
                             StickyIds({"9AAAAAAAAAAA": "9AAAAAAAAAAA"}), "US")
    assert len(rows) == 1
    row = rows[0]
    # The existing xCloud tile is kept, even over a remembered PC-only id.
    assert row.store_game_id == "9NBLGGH4PNC7"
    assert row.tags == [GameTag.XCLOUD, GameTag.BROWSER, GameTag.XBOX_PC]
    assert row.metadata["browser_url"].endswith("9NBLGGH4PNC7"), "Stream opens the xCloud product"
    assert row.metadata["xbox_pc"]["target_id"] == "9AAAAAAAAAAA", "Install uses the PC product"


def test_an_installed_pc_game_keeps_its_id_when_paired_by_name() -> None:
    sticky = StickyIds.from_sources({}, [{"productId": "9AAAAAAAAAAA", "storeGameId": "9AAAAAAAAAAA"}])
    rows = merge_with_xcloud([_xcloud("c4z7qm8fsxm2", "Hellblade: Senua's Sacrifice")],
                             [_title("9AAAAAAAAAAA", title="Hellblade: Senua's Sacrifice")], sticky, "US")
    assert [r.store_game_id for r in rows] == ["9AAAAAAAAAAA"]
    assert rows[0].metadata["browser_url"].endswith("c4z7qm8fsxm2")


def test_editions_stay_apart_and_unrelated_names_never_pair() -> None:
    rows = merge_with_xcloud(
        [_xcloud("9NBLGGH4PNC7", "Fallout 3"), _xcloud("9NBLGGH4PNC8", "Halo")],
        [_title("9AAAAAAAAAAA", title="Fallout 3: Game of the Year Edition"),
         _title("9BBBBBBBBBBB", title="Halo 2")],
        StickyIds(), "US")
    assert [r.store_game_id for r in rows] == [
        "9NBLGGH4PNC7", "9NBLGGH4PNC8", "9AAAAAAAAAAA", "9BBBBBBBBBBB"]
    assert all(GameTag.XBOX_PC not in r.tags for r in rows[:2])


def test_a_name_pair_must_be_unique() -> None:
    # Two PC titles with one name: neither pairs.
    rows = merge_with_xcloud([_xcloud("9NBLGGH4PNC7", "Prey")],
                             [_title("9AAAAAAAAAAA", title="Prey (PC)"), _title("9BBBBBBBBBBB", title="Prey")],
                             StickyIds(), "US")
    assert len(rows) == 3
    # Two plain xCloud rows with one name: the PC title pairs with neither.
    rows = merge_with_xcloud([_xcloud("9NBLGGH4PNC7", "Prey"), _xcloud("9NBLGGH4PNC8", "Prey")],
                             [_title("9AAAAAAAAAAA", title="Prey")], StickyIds(), "US")
    assert len(rows) == 3


def test_the_plainest_xcloud_sku_wins_then_series_xs() -> None:
    rows = merge_with_xcloud(
        [_xcloud("9NBLGGH4PNC7", "The Crew Motorfest - Xbox One"),
         _xcloud("9NBLGGH4PNC8", "The Crew Motorfest - Xbox Series X|S")],
        [_title("9AAAAAAAAAAA", title="The Crew Motorfest")], StickyIds(), "US")
    assert [GameTag.XBOX_PC in r.tags for r in rows] == [False, True]
    rows = merge_with_xcloud(
        [_xcloud("9NBLGGH4PNC7", "Goat Simulator 3: Xbox One Edition"),
         _xcloud("9NBLGGH4PNC8", "Goat Simulator 3")],
        [_title("9AAAAAAAAAAA", title="Goat Simulator 3: Windows Edition")], StickyIds(), "US")
    assert [GameTag.XBOX_PC in r.tags for r in rows] == [False, True]


def test_target_match_wins_over_entitlement_match() -> None:
    bundle = _title("9AAAAAAAAAAA", target="9BBBBBBBBBBB")
    direct = _title("9CCCCCCCCCCC", target="9AAAAAAAAAAA")
    rows = merge_with_xcloud([_xcloud("9AAAAAAAAAAA")], [bundle, direct], StickyIds(), "US")
    assert rows[0].metadata["xbox_pc"]["product_id"] == "9CCCCCCCCCCC"
    assert rows[1].store_game_id == "9BBBBBBBBBBB"


def test_no_titles_leaves_xcloud_rows_untouched() -> None:
    games = [_xcloud("9NBLGGH4PNC7")]
    assert merge_with_xcloud(games, [], StickyIds(), "US") == games


def test_game_pass_key_only_for_owned_and_gamepass() -> None:
    rows = merge_with_xcloud([], [_title("9AAAAAAAAAAA", access="owned"),
                                  _title("9BBBBBBBBBBB", access="ubisoft_plus", state="not_supported")],
                             StickyIds(), "US")
    assert rows[0].metadata[GAME_PASS_KEY] is False
    assert GAME_PASS_KEY not in rows[1].metadata


def test_sticky_ids_survive_a_case_change_and_a_target_flip() -> None:
    sticky = StickyIds({"9ABCDEFGHIJK": "9abcdefghijk"})
    # The xCloud row now arrives upper-case: the tile keeps its old key.
    rows = merge_with_xcloud([_xcloud("9ABCDEFGHIJK")], [_title("9ABCDEFGHIJK")], sticky, "US")
    assert rows[0].store_game_id == "9abcdefghijk"
    # A choice row keyed by its entitlement becomes ready on another target.
    sticky = StickyIds({"9AAAAAAAAAAA": "9AAAAAAAAAAA"})
    rows = merge_with_xcloud([], [_title("9AAAAAAAAAAA", target="9BBBBBBBBBBB")], sticky, "US")
    assert rows[0].store_game_id == "9AAAAAAAAAAA"


def test_record_store_game_id_beats_the_remembered_map() -> None:
    sticky = StickyIds.from_sources({"9AAAAAAAAAAA": "old"},
                                    [{"productId": "9AAAAAAAAAAA", "storeGameId": "9aaaaaaaaaaa"}])
    assert sticky.lookup("9AAAAAAAAAAA") == "9aaaaaaaaaaa"


def test_dedupe_prefers_owned_then_ready() -> None:
    titles = dedupe_titles([
        _title("9AAAAAAAAAAA", target="9TTTTTTTTTTT", access="gamepass"),
        _title("9BBBBBBBBBBB", target="9TTTTTTTTTTT", access="owned", state="choice"),
        _title("9CCCCCCCCCCC", target="9TTTTTTTTTTT", access="owned"),
    ])
    assert [t.entitlement_id for t in titles] == ["9CCCCCCCCCCC"]


def test_floor_adds_rows_for_unlisted_records_and_upgrades_xcloud_rows() -> None:
    records = {"9AAAAAAAAAAA": {"productId": "9AAAAAAAAAAA", "market": "IN", "contentId": CID},
               "9NBLGGH4PNC7": {"productId": "9NBLGGH4PNC7"}}
    last = {"9AAAAAAAAAAA": _title("9AAAAAAAAAAA", title="Balatro")}
    rows = installed_floor([_xcloud("9nblggh4pnc7")], records, StickyIds(), last, "US")
    assert len(rows) == 2
    upgraded, added = rows
    assert upgraded.store_game_id == "9nblggh4pnc7"
    assert GameTag.XBOX_PC in upgraded.tags and GameTag.XCLOUD in upgraded.tags
    assert upgraded.metadata["xbox_pc"]["state"] == "license_not_confirmed"
    assert added.title == "Balatro" and added.tags == [GameTag.XBOX_PC]
    assert added.metadata["xbox_pc"]["reason"] == "entitlement_missing"
    assert added.metadata["xbox_pc"]["market"] == "IN"


def test_floor_skips_records_a_pc_row_already_covers() -> None:
    rows = merge_with_xcloud([], [_title("9AAAAAAAAAAA", target="9BBBBBBBBBBB")], StickyIds(), "US")
    out = installed_floor(rows, {"9AAAAAAAAAAA": {"productId": "9AAAAAAAAAAA"}}, StickyIds(), {}, "US")
    assert len(out) == 1


def test_overlay_maps_rows_to_verified_records() -> None:
    rows = merge_with_xcloud([_xcloud("9pk087lngjc5"), _xcloud("9NBLGGH4PNC7")],
                             [_title("9PK087LNGJC5")], StickyIds(), "US")
    record = {"productId": "9PK087LNGJC5", "installRoot": "/x", "exe": "/x/game/a.exe"}
    overlay = installed_overlay(rows, {"9PK087LNGJC5": record, "9NBLGGH4PNC7": record})
    # The plain xCloud row never takes install state.
    assert overlay == {"9pk087lngjc5": record}


def test_summarize_roots_sorts_unknown_absent_and_unavailable() -> None:
    game = {"ProductId": "9AAAAAAAAAAA", "ProductType": "Game",
            "LocalizedProperties": [{"ProductTitle": "Bundle"}],
            "DisplaySkuAvailabilities": [{"Sku": {"Properties": {"BundledSkus": [
                {"BigId": "9BBBBBBBBBBB"}]}}}]}
    other = {**game, "ProductId": "9CCCCCCCCCCC", "DisplaySkuAvailabilities": [
        {"Sku": {"Properties": {"BundledSkus": [{"BigId": "9FFFFFFFFFFF"}]}}}]}
    found = PlanProducts(products={"9AAAAAAAAAAA": game, "9CCCCCCCCCCC": other},
                         unknown={"9BBBBBBBBBBB", "9DDDDDDDDDDD"}, absent={"9EEEEEEEEEEE"})
    summaries, unknown = summarize_roots(
        ["9AAAAAAAAAAA", "9CCCCCCCCCCC", "9DDDDDDDDDDD", "9EEEEEEEEEEE"], found)
    # A: its child is unknown. C: its child is in neither set, so unavailable.
    assert unknown == {"9AAAAAAAAAAA", "9DDDDDDDDDDD"}
    assert set(summaries) == {"9CCCCCCCCCCC"}
    assert summaries["9CCCCCCCCCCC"]["status"] == "unavailable"
