"""Tests for the ``update_steam_owned_titles`` RPC (C.11).

Instantiates the mixin directly against a minimal host, matching the
other RPC tests (see ``test_support_bundle_rpc.py``). Because
``@auto_wrap_rpc_methods`` is applied to the ``Plugin`` class rather than
to the mixins, the coroutine under test is unwrapped here — assertions
are on the raw return value, not the ``{success, error, data}`` envelope
the frontend sees.
"""
from __future__ import annotations

import asyncio

from unifideck.rpc.mixins.sync import SyncRPCMixin


class _FakeSyncService:
    """Records whether the re-annotate/re-save pair ran (C.11)."""

    def __init__(self) -> None:
        self.annotate_calls = 0
        self.save_calls = 0

    def _annotate_loaded_cache(self) -> None:
        self.annotate_calls += 1

    def _save_library_cache(self) -> None:
        self.save_calls += 1


def _make_host(sync_service: object | None) -> SyncRPCMixin:
    host = SyncRPCMixin()
    host.sync_service = sync_service
    host.config = None
    return host


def test_update_steam_owned_titles_reannotates_and_resaves(tmp_path, monkeypatch):
    """C.11 — pushing a fresh Steam-owned snapshot must take effect
    immediately (re-stamp dedupe_group_id/steam_owned_app_id on the live
    in-memory games and persist it), not only at the next full sync."""
    from unifideck.steam import owned_games

    monkeypatch.setattr(owned_games, "_FRONTEND_CACHE_PATH", tmp_path / "titles.json")
    monkeypatch.setattr(owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json")

    fake_sync_service = _FakeSyncService()
    host = _make_host(fake_sync_service)

    result = asyncio.run(
        host.update_steam_owned_titles(
            [{"title": "Disco Elysium", "appid": 632470}],
        ),
    )

    assert result == {"count": 1}
    assert fake_sync_service.annotate_calls == 1
    assert fake_sync_service.save_calls == 1


def test_update_steam_owned_titles_tolerates_no_sync_service(tmp_path, monkeypatch):
    """A push that arrives before sync_service is wired up (or after it
    was torn down) must still persist the titles — the re-annotate step
    is a bonus for immediacy, not a hard dependency."""
    from unifideck.steam import owned_games

    monkeypatch.setattr(owned_games, "_FRONTEND_CACHE_PATH", tmp_path / "titles.json")
    monkeypatch.setattr(owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json")

    host = _make_host(None)

    result = asyncio.run(
        host.update_steam_owned_titles(
            [{"title": "Disco Elysium", "appid": 632470}],
        ),
    )

    assert result == {"count": 1}
    assert owned_games.load_frontend_owned_games() == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="Disco Elysium"),
    }
