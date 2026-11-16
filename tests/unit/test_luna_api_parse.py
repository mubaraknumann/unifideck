"""``luna_api.parse``: the ``getPage`` "my games" response → Luna titles.

Runs against ``tests/fixtures/luna/getpage_my_games.json``, a trimmed copy of
a real response (6 of 88 tiles, page tokens replaced by dummies), plus small
hand-built payloads for the shape-drift and malformed-tile cases.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.luna.luna_api import LunaApiShapeError, parse_library_page

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "luna" / "getpage_my_games.json"
_HOST = "https://luna.amazon.de"


@pytest.fixture
def payload() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _grid(payload: dict[str, Any]) -> dict[str, Any]:
    grid: dict[str, Any] = payload["pageMemberGroups"]["mainContent"]["widgets"][0]
    return grid


def _tile(game_id: str, title: str, target: str) -> dict[str, Any]:
    return {
        "type": "GAME_TILE",
        "id": f"game_tile_{game_id}",
        "actions": [{"target": target}],
        "presentationData": json.dumps({"gameId": game_id, "title": title}),
    }


def _page(*widgets: dict[str, Any]) -> dict[str, Any]:
    return {"pageMemberGroups": {"mainContent": {"widgets": list(widgets)}}}


# ── the real fixture ─────────────────────────────────────────────────


def test_fixture_parses_every_tile(payload: dict[str, Any]) -> None:
    library = parse_library_page(payload, _HOST)

    assert library.malformed == 0
    assert len(library.titles) == 6
    ids = [t.luna_product_id for t in library.titles]
    assert len(set(ids)) == 6
    assert any(i.startswith("amzn1.adg.product.") for i in ids)
    assert any(i.startswith("amzn1.luna.product.") for i in ids)


def test_fixture_urls_and_asins(payload: dict[str, Any]) -> None:
    by_name = {t.name: t for t in parse_library_page(payload, _HOST).titles}

    marbles = by_name["Marbles On Stream"]
    assert marbles.luna_product_id == "amzn1.luna.product.df7a7c1a-5c9c-467e-9e39-1dc93c2628aa"
    assert marbles.browser_url == f"{_HOST}/game/marbles-on-stream/B0HPX7NT7F"
    assert marbles.asin == "B0HPX7NT7F"


def test_tile_without_asin_in_path(payload: dict[str, Any]) -> None:
    """``/game/fortnite`` has no ASIN segment; the tile is still valid."""
    fortnite = next(
        t for t in parse_library_page(payload, _HOST).titles
        if t.browser_url == f"{_HOST}/game/fortnite"
    )
    assert fortnite.luna_product_id.startswith("amzn1.adg.product.")


def test_asin_falls_back_to_product_url() -> None:
    tile = _tile("amzn1.adg.product.x", "X", "/game/x")
    tile["presentationData"] = json.dumps({
        "gameId": "amzn1.adg.product.x", "title": "X",
        "hoverDetails": {"productUrl": "https://play.amazon.de/play?asin=B0ZZZZZZZZ&territory=DE"},
    })
    grid = {"id": "collection_browse_my_games", "type": "GRID", "widgets": [tile]}
    (title,) = parse_library_page(_page(grid), _HOST).titles
    assert title.asin == "B0ZZZZZZZZ"


def test_trailing_slash_on_host_is_ignored(payload: dict[str, Any]) -> None:
    titles = parse_library_page(payload, _HOST + "/").titles
    assert all(t.browser_url.startswith(_HOST + "/game/") for t in titles)


def test_fixture_contains_no_secrets() -> None:
    text = _FIXTURE.read_text(encoding="utf-8")
    for marker in ("Atza|", "eyJ", "session-id", "session-token", "x-amz-", "tdi",
                   "at-main", "sess-at-main", "ubid-main", "searchId"):
        assert marker not in text, marker
    # No long opaque base64-ish strings (tokens) anywhere in the fixture.
    assert not re.search(r"[A-Za-z0-9+/=_-]{120,}", text)


# ── shape drift / empty / malformed ──────────────────────────────────


@pytest.mark.parametrize("broken", [
    {},
    {"pageMemberGroups": {}},
    {"pageMemberGroups": {"mainContent": {"widgets": "nope"}}},
    _page({"id": "something_else", "type": "CAROUSEL", "widgets": []}),
    [],
])
def test_missing_grid_is_shape_drift(broken: Any) -> None:
    with pytest.raises(LunaApiShapeError):
        parse_library_page(broken, _HOST)


def test_grid_found_by_type_when_id_changes(payload: dict[str, Any]) -> None:
    renamed = copy.deepcopy(payload)
    _grid(renamed)["id"] = "collection_browse_my_games_v2"
    assert len(parse_library_page(renamed, _HOST).titles) == 6


def test_empty_grid_is_an_empty_library() -> None:
    grid = {"id": "collection_browse_my_games", "type": "GRID", "widgets": []}
    library = parse_library_page(_page(grid), _HOST)
    assert library.titles == []
    assert library.malformed == 0


@pytest.mark.parametrize("broken_tile", [
    {"type": "GAME_TILE", "id": "game_tile_x", "actions": [], "presentationData": "{}"},
    {"type": "GAME_TILE", "id": "", "actions": [{"target": "/game/x"}],
     "presentationData": json.dumps({"title": "X"})},
    {"type": "GAME_TILE", "id": "game_tile_x", "actions": [{"target": "/browse"}],
     "presentationData": json.dumps({"title": "X"})},
    {"type": "GAME_TILE", "id": "game_tile_x", "actions": [{"target": "/game/x"}],
     "presentationData": "{not json"},
    {"type": "GAME_TILE", "id": "game_tile_x", "actions": [{"target": "/game/x"}],
     "presentationData": {"title": "X"}},
])
def test_malformed_tile_is_counted_and_skipped(broken_tile: dict[str, Any]) -> None:
    good = _tile("amzn1.adg.product.ok", "Ok", "/game/ok/B0OKOKOKOK")
    grid = {"id": "collection_browse_my_games", "type": "GRID", "widgets": [good, broken_tile]}
    library = parse_library_page(_page(grid), _HOST)
    assert [t.luna_product_id for t in library.titles] == ["amzn1.adg.product.ok"]
    assert library.malformed == 1


def test_id_falls_back_to_tile_id() -> None:
    tile = _tile("amzn1.adg.product.y", "Y", "/game/y/B0YYYYYYYY")
    tile["presentationData"] = json.dumps({"title": "Y"})
    grid = {"id": "collection_browse_my_games", "type": "GRID", "widgets": [tile]}
    (title,) = parse_library_page(_page(grid), _HOST).titles
    assert title.luna_product_id == "amzn1.adg.product.y"


def test_unexpected_grid_content_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    good = _tile("amzn1.adg.product.ok", "Ok", "/game/ok/B0OKOKOKOK")
    grid = {
        "id": "collection_browse_my_games", "type": "GRID",
        "widgets": [good, {"type": "LOAD_MORE"}], "actions": [{"target": "/more"}],
    }
    with caplog.at_level("WARNING"):
        library = parse_library_page(_page(grid), _HOST)
    assert len(library.titles) == 1
    assert "pagination is not implemented" in caplog.text
