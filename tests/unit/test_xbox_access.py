"""The ribbon's access gate for Microsoft rows (``services/microsoft_ownership/access``).

A row only says what was true at its last sync, so every claim needs
evidence that is still current: an Xbox PC entitlement whose end date is
ahead (a Game Pass one ends with the subscription period), or an xCloud
answer at most a week old from an account that still has a plan.
"""
from __future__ import annotations

from typing import Any

from unifideck.core.cross_store_ownership import GAME_PASS_KEY, RowAccess
from unifideck.core.types import Game, GameTag
from unifideck.services.microsoft_ownership.access import (
    STREAM_EVIDENCE_MAX_AGE,
    row_access,
    xbox_access,
)
from unifideck.stores.microsoft.microsoft_catalog import (
    XCLOUD_CHECKED_AT_KEY,
    XCLOUD_PLAYABLE_KEY,
    MicrosoftCatalogReader,
)

NOW = 1_793_777_718.0  # 2026-11-04
AHEAD = "2026-12-01T00:00:00.000000000Z"
PAST = "2026-10-01T00:00:00Z"
FOREVER = "9999-12-31T23:59:59.9999999Z"


def _pc(access: str, end: str = AHEAD, state: str = "ready") -> dict[str, Any]:
    return {"xbox_pc": {"v": 1, "access": access, "state": state, "end_date": end,
                        "product_id": "9ENT00000001", "target_id": "9TGT00000001"}}


def _row(*, pc: dict[str, Any] | None = None, xcloud: bool = False, **meta: Any) -> Game:
    tags = ([GameTag.XBOX_PC] if pc else []) + ([GameTag.XCLOUD] if xcloud else [])
    return Game(app_id=0, store="microsoft", store_game_id="9ROW00000001", title="T",
                tags=tags, metadata={**(pc or {}), **meta})


def _fresh(**meta: Any) -> dict[str, Any]:
    return {XCLOUD_CHECKED_AT_KEY: NOW - 3600, **meta}


def test_an_owned_pc_licence_is_owned_while_it_lasts() -> None:
    assert row_access(_row(pc=_pc("owned", FOREVER)), NOW).owned_pc
    assert not row_access(_row(pc=_pc("owned", PAST)), NOW).owned_pc


def test_a_game_pass_pc_title_is_installable_until_the_plan_period_ends() -> None:
    live = row_access(_row(pc=_pc("gamepass")), NOW)
    assert (live.pass_pc, live.owned_pc, live.streams) == (True, False, False)
    assert not row_access(_row(pc=_pc("gamepass", PAST)), NOW).pass_pc


def test_unsupported_unconfirmed_or_undated_entitlements_claim_nothing() -> None:
    for pc in (_pc("ea_play"), _pc("ubisoft_plus"), _pc("none"),
               _pc("gamepass", state="not_supported"),
               _pc("owned", state="license_not_confirmed"), _pc("owned", end="")):
        assert row_access(_row(pc=pc), NOW) == RowAccess(
            product_ids=frozenset({"9ROW00000001", "9ENT00000001", "9TGT00000001"}),
        )


def test_xbox_pc_meta_without_the_tag_is_ignored() -> None:
    game = Game(app_id=0, store="microsoft", store_game_id="X", title="T", metadata=_pc("owned"))
    assert not row_access(game, NOW).owned_pc


def test_a_recent_xcloud_answer_streams() -> None:
    assert row_access(_row(xcloud=True, **_fresh(**{GAME_PASS_KEY: True})), NOW).streams


def test_stale_or_unstamped_xcloud_rows_do_not_stream() -> None:
    old = {XCLOUD_CHECKED_AT_KEY: NOW - STREAM_EVIDENCE_MAX_AGE - 1}
    assert not row_access(_row(xcloud=True, **old), NOW).streams
    assert not row_access(_row(xcloud=True), NOW).streams
    assert not row_access(_row(xcloud=True, **{XCLOUD_CHECKED_AT_KEY: True}), NOW).streams
    ahead = {XCLOUD_CHECKED_AT_KEY: NOW + 3600}
    assert not row_access(_row(xcloud=True, **ahead), NOW).streams


def test_spent_play_time_does_not_stream() -> None:
    game = _row(xcloud=True, **_fresh(**{XCLOUD_PLAYABLE_KEY: False}))
    assert not row_access(game, NOW).streams


def test_an_owned_title_outside_game_pass_streams_even_with_a_lapsed_pass_row() -> None:
    game = _row(pc=_pc("gamepass", PAST), xcloud=True, **_fresh(**{GAME_PASS_KEY: False}))
    assert row_access(game, NOW).streams


def test_a_lapsed_game_pass_pc_entitlement_stops_streaming_too() -> None:
    game = _row(pc=_pc("gamepass", PAST), xcloud=True, **_fresh(**{GAME_PASS_KEY: True}))
    assert row_access(game, NOW) == RowAccess(
        product_ids=frozenset({"9ROW00000001", "9ENT00000001", "9TGT00000001"}),
    )


def test_xbox_access_keys_microsoft_rows_by_upper_case_id() -> None:
    other = Game(app_id=1, store="gog", store_game_id="g", title="G")
    row = Game(app_id=0, store="microsoft", store_game_id="9abc", title="T")
    assert set(xbox_access([row, other], NOW)) == {"9ABC"}


def test_the_catalog_stamps_each_row_and_marks_spent_play_time() -> None:
    entitled = [
        {"titleId": "A", "details": {"productId": "9A", "hasEntitlement": True}},
        {"titleId": "B", "details": {"productId": "9B", "hasEntitlement": True,
                                     "remainingGameplayTimeInSeconds": 0}},
        {"titleId": "C", "details": {"productId": "9C", "hasEntitlement": True,
                                     "remainingGameplayTimeInSeconds": 1200}},
    ]
    a, b, c = MicrosoftCatalogReader._build_xcloud_games(entitled, {}, None, checked_at=123.0)
    assert [g.metadata[XCLOUD_CHECKED_AT_KEY] for g in (a, b, c)] == [123.0] * 3
    assert XCLOUD_PLAYABLE_KEY not in a.metadata
    assert b.metadata[XCLOUD_PLAYABLE_KEY] is False
    assert XCLOUD_PLAYABLE_KEY not in c.metadata
