"""Tests for the ``update_steam_owned_titles`` RPC.

Instantiates the mixin directly against a minimal host, matching the
other RPC tests (see ``test_support_bundle_rpc.py``). Because
``@auto_wrap_rpc_methods`` is applied to the ``Plugin`` class rather than
to the mixins, the coroutine under test is unwrapped here — assertions
are on the raw return value, not the ``{success, error, data}`` envelope
the frontend sees.
"""
from __future__ import annotations

import asyncio
import json

from unifideck.rpc.mixins.sync import SyncRPCMixin


class _FakeSyncService:
    """Records whether the library was re-grouped."""

    def __init__(self) -> None:
        self.refresh_calls = 0

    async def refresh_duplicate_groups(self) -> None:
        self.refresh_calls += 1


def _make_host(sync_service: object | None) -> SyncRPCMixin:
    host = SyncRPCMixin()
    host.sync_service = sync_service
    host.config = None
    return host


def _isolate_caches(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from unifideck.steam import owned_games

    monkeypatch.setattr(owned_games, "_FRONTEND_CACHE_PATH", tmp_path / "titles.json")
    monkeypatch.setattr(owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json")
    return owned_games


def test_update_steam_owned_titles_regroups_the_library(tmp_path, monkeypatch):
    """A fresh Steam library takes effect now, not at the next sync."""
    _isolate_caches(tmp_path, monkeypatch)
    fake_sync_service = _FakeSyncService()
    host = _make_host(fake_sync_service)

    result = asyncio.run(
        host.update_steam_owned_titles([{"title": "Disco Elysium", "appid": 632470}]),
    )

    assert result == {"count": 1}
    assert fake_sync_service.refresh_calls == 1


def test_update_steam_owned_titles_tolerates_no_sync_service(tmp_path, monkeypatch):
    """A push that arrives before sync_service is wired up (or after it
    was torn down) must still persist the titles — the re-group step is a
    bonus for immediacy, not a hard dependency."""
    owned_games = _isolate_caches(tmp_path, monkeypatch)
    host = _make_host(None)

    result = asyncio.run(
        host.update_steam_owned_titles([{"title": "Disco Elysium", "appid": 632470}]),
    )

    assert result == {"count": 1}
    assert owned_games.load_frontend_owned_games() == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="Disco Elysium"),
    }


def test_a_plain_title_list_keeps_both_caches(tmp_path, monkeypatch):
    """The payload before PR #461 was a list of titles. It still updates
    the title cache, and must not wipe the title + appid cache."""
    owned_games = _isolate_caches(tmp_path, monkeypatch)
    host = _make_host(None)
    asyncio.run(host.update_steam_owned_titles([{"title": "Disco Elysium", "appid": 632470}]))

    result = asyncio.run(host.update_steam_owned_titles(["Hades", "Celeste"]))

    assert result == {"count": 2}
    titles = json.loads((tmp_path / "titles.json").read_text(encoding="utf-8"))["titles"]
    assert set(titles) == {"hades", "celeste"}
    assert set(owned_games.load_frontend_owned_games()) == {"disco elysium"}
