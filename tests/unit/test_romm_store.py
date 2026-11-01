"""RomMStore: availability, library failure, and the install lifecycle.

The failure cases are the point. ``get_library`` returns ``None`` rather than
an empty list, a 401 makes the store unavailable, and uninstall emits
``GAME_UNINSTALLED`` exactly once, which is what lets ShortcutService flip the
shortcut back without losing its appid or artwork.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.romm_fakes import Reply, romm_server  # noqa: F401
from unifideck.core.types import Events
from unifideck.stores.romm.store import RomMStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    games_map = tmp_path / "games.map"
    games_map.write_text("")
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path", lambda config=None: str(games_map))
    cfg_file = tmp_path / "romm_config.json"
    cfg_file.write_text(json.dumps({"server_url": "https://romm.test", "token": "fixture", "excluded_fs_slugs": []}))
    config = MagicMock()
    config.get.return_value = {
        "config_file": str(cfg_file),
        "default_install_root": str(tmp_path / "games"),
        "download_dir": str(tmp_path / "dl"),
    }
    bus = MagicMock()
    bus.emit = AsyncMock()
    return RomMStore(bus, MagicMock(), plugin_dir=None, config=config)


def test_store_info_declares_romm():
    info = RomMStore.store_info
    assert (info.name, info.display_name, info.auth_method) == ("romm", "RomM", "manual")
    assert info.icon_asset == "" and info.supports_install is True


async def test_is_available_reflects_committed_state_only(store, romm_server):
    assert await store.is_available() is True
    assert romm_server.calls == []


async def test_library_failure_returns_none(store, romm_server):
    romm_server.on("GET", "/api/platforms", Reply(status=503))
    assert await store.get_library() is None
    assert await store.is_available() is True


async def test_unauthorized_library_marks_store_unavailable_until_reconnect(store, romm_server):
    romm_server.on("GET", "/api/platforms", Reply(status=401))
    assert await store.get_library() is None
    assert await store.is_available() is False


async def test_unexpected_exception_returns_none(store, monkeypatch):
    monkeypatch.setattr(store._library_reader, "get_library", AsyncMock(side_effect=ValueError("boom")))
    assert await store.get_library() is None


async def test_complete_library_overlays_install_state(store, romm_server):
    romm_server.on("GET", "/api/platforms", Reply(json=[{"id": 1, "slug": "win", "fs_slug": "win"}]))
    romm_server.on("GET", "/api/roms", Reply(json={"items": [
        {"id": 5, "platform_slug": "win", "platform_fs_slug": "win", "name": "G", "fs_name": "G.zip"},
    ], "total": 1}))
    game_id = "1ad0c8ff8989849d4235d070383964a906b3ddaa73b0cf69d37dc9d00066eda5_5"
    store._installer._records.save(game_id, title="G", install_path="/games/G", exe_path="/games/G/G.exe")
    games = await store.get_library()
    assert games is not None and games[0].installed and games[0].install_path == "/games/G"
    assert not games[0].exe_path
    assert await store.get_installed_path(game_id) == "/games/G"


async def test_uninstall_emits_game_uninstalled_once(store, tmp_path):
    target = tmp_path / "games" / "G"
    target.mkdir(parents=True)
    (target / "G.exe").write_bytes(b"MZ")
    store._installer._records.save("5", title="G", install_path=str(target), exe_path=str(target / "G.exe"))
    result = await store.uninstall_game("5")
    assert result.success
    store._bus.emit.assert_awaited_once_with(Events.GAME_UNINSTALLED, store="romm", game_id="5")


async def test_failed_uninstall_does_not_emit(store):
    result = await store.uninstall_game("404")
    assert not result.success
    store._bus.emit.assert_not_awaited()


async def test_no_automatic_updates(store):
    assert await store.check_for_updates() == []


async def test_update_uses_existing_reinstall_contract(store, monkeypatch):
    install = AsyncMock(return_value="installed")
    monkeypatch.setattr(store._installer, "install_game", install)
    assert await store.update_game("5", base_path="/games") == "installed"
    install.assert_awaited_once_with("5", install_path="/games", progress_callback=None)


async def test_install_accepts_gamevault_style_aliases(store, monkeypatch):
    install = AsyncMock(return_value="ok")
    monkeypatch.setattr(store._installer, "install_game", install)
    cb = AsyncMock()
    await store.install_game("5", progress_cb=cb, install_path="/x")
    install.assert_awaited_once_with("5", install_path="/x", progress_callback=cb)


async def test_cancel_auth_without_id_cancels_pending_and_keeps_connection(store):
    result = await store.cancel_auth()
    assert result.success and await store.is_available()


async def test_logout_clears_connection(store, romm_server):
    result = await store.logout()
    assert result.success and not await store.is_available()


async def test_shutdown_releases_pending_state(store):
    await store.shutdown()
    assert await store.is_available()


async def test_registry_cancel_forwards_kwargs() -> None:
    from unittest.mock import AsyncMock

    from unifideck.core.types import AuthResult
    from unifideck.stores.shared.store_registry import StoreRegistry

    store = AsyncMock()
    store.cancel_auth = AsyncMock(return_value=AuthResult(success=True, store="romm"))
    registry = StoreRegistry.__new__(StoreRegistry)
    registry._stores = {"romm": store}
    registry.get = lambda name: store  # type: ignore[method-assign]
    await registry.auth_action("romm", "cancel", attempt_id="a1")
    store.cancel_auth.assert_awaited_once_with(attempt_id="a1")
