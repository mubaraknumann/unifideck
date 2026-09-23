"""Tests for ``stores.itch.library``: owned keys → games, installs overlaid.

Fixtures are the shapes butler 15.31.0 returned for a real account
(2026-09-23), trimmed: ``platforms`` values are strings, a ``type: "html"``
game can still be a native download, and one owned item is an asset pack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.itch import library as lib_mod
from unifideck.stores.itch.butlerd import ButlerdError
from unifideck.stores.itch.library import ItchLibraryReader, is_runnable_game, to_game


def _game(gid: int, title: str, **kw: Any) -> dict[str, Any]:
    return {"id": gid, "title": title, "classification": kw.get("classification", "game"),
            "type": kw.get("type", "default"), "platforms": kw.get("platforms", {"windows": "all"}),
            "coverUrl": f"https://img.itch.zone/{gid}.png", "url": f"https://dev.itch.io/{gid}"}


class _FakeDaemon:
    """Scripted ``ButlerDaemon.call``: a method → list-of-answers table."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        defaults: dict[str, list[Any]] = {"Fetch.ProfileCollections": [{"items": []}]}
        self.script = {k: list(v) for k, v in {**defaults, **script}.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        answer = self.script[method].pop(0) if len(self.script[method]) > 1 else self.script[method][0]
        if isinstance(answer, Exception):
            raise answer
        return answer


_PROFILE = {"profiles": [{"id": 42}]}
_NO_CAVES = {"items": []}


def test_only_runnable_games_become_shortcuts() -> None:
    assert is_runnable_game(_game(1, "Win", platforms={"windows": "all"}))
    assert is_runnable_game(_game(2, "Html shell, native build", type="html",
                                  platforms={"linux": "all", "windows": "all", "osx": "all"}))
    assert not is_runnable_game(_game(3, "Paintings", classification="assets",
                                      platforms={"windows": "all", "linux": "all"}))
    assert not is_runnable_game(_game(4, "Mac only", platforms={"osx": "all"}))
    assert not is_runnable_game(_game(5, "No platforms", platforms={}))


def test_to_game_maps_id_title_and_cover() -> None:
    g = to_game(_game(2119837, "Fear Assessment", platforms={"linux": "all", "windows": "all"}))
    assert (g.store, g.store_game_id, g.title) == ("itch", "2119837", "Fear Assessment")
    assert g.icon_url == "https://img.itch.zone/2119837.png"
    assert g.metadata["platforms"] == ["linux", "windows"]


async def test_library_pages_through_owned_keys_with_fresh() -> None:
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [
            {"items": [{"game": _game(1, "A")}], "nextCursor": "c1"},
            {"items": [{"game": _game(2, "B")}, {"game": _game(3, "Art", classification="assets")}]},
        ],
        "Fetch.Caves": [_NO_CAVES],
    })
    games = await ItchLibraryReader(daemon).get_library()
    assert [g.store_game_id for g in games or []] == ["1", "2"]
    owned_calls = [p for m, p in daemon.calls if m == "Fetch.ProfileOwnedKeys"]
    # Without fresh butler answers [] from an empty cache on a new DB.
    assert all(p["fresh"] is True and p["profileId"] == 42 for p in owned_calls)
    assert owned_calls[1]["cursor"] == "c1"


async def test_installed_caves_overlay_install_state(tmp_path: Path) -> None:
    folder = tmp_path / "fear-assessment"
    folder.mkdir()
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": _game(1, "A")}, {"game": _game(2, "B")}]}],
        "Fetch.Caves": [{"items": [
            {"id": "cave-1", "game": {"id": 1, "title": "A"}, "installInfo": {"installFolder": str(folder)}},
            # butler's record outlives the folder: must NOT read as installed.
            {"id": "cave-2", "game": {"id": 2, "title": "B"},
             "installInfo": {"installFolder": str(tmp_path / "gone")}},
        ]}],
    })
    games = {g.store_game_id: g for g in await ItchLibraryReader(daemon).get_library() or []}
    assert games["1"].installed and games["1"].install_path == str(folder)
    assert not games["2"].installed


@pytest.mark.parametrize("failure", [
    ButlerdError("butlerd connection lost"),
    ButlerdError("itch.io API error (401)", code=12000, api_status=401),
])
async def test_unreadable_library_is_none_never_empty(failure: Exception) -> None:
    daemon = _FakeDaemon({"Profile.List": [_PROFILE], "Fetch.ProfileOwnedKeys": [failure],
                          "Fetch.Caves": [_NO_CAVES]})
    assert await ItchLibraryReader(daemon).get_library() is None


async def test_no_profile_is_none() -> None:
    daemon = _FakeDaemon({"Profile.List": [{"profiles": []}]})
    assert await ItchLibraryReader(daemon).get_library() is None


async def test_rate_limit_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(lib_mod.asyncio, "sleep", fake_sleep)
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [ButlerdError("429", code=12000, api_status=429),
                                   {"items": [{"game": _game(1, "A")}]}],
        "Fetch.Caves": [_NO_CAVES],
    })
    games = await ItchLibraryReader(daemon).get_library()
    assert [g.store_game_id for g in games or []] == ["1"]
    assert slept == [2.0]


# ── collections ────────────────────────────────────────────────────────
async def test_free_collection_games_join_the_owned_ones() -> None:
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": {**_game(1, "Owned"), "minPrice": 499}}]}],
        "Fetch.ProfileCollections": [{"items": [{"id": 8205606, "title": "Mine"}]}],
        "Fetch.Collection.Games": [{"items": [
            {"game": _game(2, "Free one")},                       # no minPrice = free
            {"game": {**_game(3, "Paid, not bought"), "minPrice": 999}},
            {"game": {**_game(1, "Owned"), "minPrice": 499}},     # already owned
        ]}],
        "Fetch.Caves": [_NO_CAVES],
    })
    games = {g.store_game_id: g for g in await ItchLibraryReader(daemon).get_library() or []}
    assert set(games) == {"1", "2"}
    assert games["1"].metadata["source"] == "owned"
    assert games["2"].metadata["source"] == "collection"
    games_call = next(p for m, p in daemon.calls if m == "Fetch.Collection.Games")
    assert games_call["collectionId"] == 8205606 and games_call["fresh"] is True


async def test_a_failed_collection_read_keeps_every_shortcut() -> None:
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": _game(1, "Owned")}]}],
        "Fetch.ProfileCollections": [ButlerdError("butlerd connection lost")],
        "Fetch.Caves": [_NO_CAVES],
    })
    # Answering with the owned games alone would delete the collection
    # games' shortcuts, so the whole read is "unreadable".
    assert await ItchLibraryReader(daemon).get_library() is None


# ── untagged games ─────────────────────────────────────────────────────
async def test_untagged_game_is_kept_when_an_upload_names_a_build() -> None:
    deltarune = _game(1193828, "DELTARUNE", platforms={})
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": deltarune}]}],
        "Fetch.GameUploads": [{"uploads": [
            {"id": 1, "type": "default", "storage": "hosted", "platforms": {},
             "filename": "LTS DELTARUNE Chapter 1&2 (PC Version 3).zip"},
            {"id": 2, "type": "default", "storage": "hosted", "platforms": {},
             "filename": "LTS DELTARUNE Chapter 1&2 (Mac Version 3).zip"},
        ]}],
        "Fetch.Caves": [_NO_CAVES],
    })
    games = await ItchLibraryReader(daemon).get_library() or []
    assert [(g.store_game_id, g.tags) for g in games] == [("1193828", [])]


async def test_html_only_game_becomes_a_web_game() -> None:
    survivors = _game(1242468, "Vampire Survivors", type="html", platforms={})
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": survivors}]}],
        "Fetch.GameUploads": [{"uploads": [
            {"id": 9, "type": "html", "storage": "hosted", "platforms": {}, "filename": "web.zip"},
        ]}],
        "Fetch.Caves": [_NO_CAVES],
    })
    games = await ItchLibraryReader(daemon).get_library() or []
    assert [(g.store_game_id, g.tags) for g in games] == [("1242468", ["browser"])]
    # The page is where itch.io runs the game; the launcher opens it in Edge.
    assert games[0].metadata["browser_url"] == "https://dev.itch.io/1242468"


async def test_untagged_game_with_nothing_runnable_is_left_out() -> None:
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": _game(5, "Mac only", platforms={})}]}],
        "Fetch.GameUploads": [{"uploads": [
            {"id": 1, "type": "default", "storage": "hosted", "platforms": {}, "filename": "game-osx.zip"},
        ]}],
        "Fetch.Caves": [_NO_CAVES],
    })
    assert await ItchLibraryReader(daemon).get_library() == []


async def test_tagged_games_cost_no_upload_lookups() -> None:
    daemon = _FakeDaemon({
        "Profile.List": [_PROFILE],
        "Fetch.ProfileOwnedKeys": [{"items": [{"game": _game(1, "Tagged")}]}],
        "Fetch.Caves": [_NO_CAVES],
    })
    await ItchLibraryReader(daemon).get_library()
    assert "Fetch.GameUploads" not in [m for m, _ in daemon.calls]
