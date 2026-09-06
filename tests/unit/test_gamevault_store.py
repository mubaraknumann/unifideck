"""Tests for ``stores.gamevault.store.GameVaultStore`` — orchestration
logic between auth/installer/library-reader, with those collaborators
mocked out (their own behaviour is covered by
test_gamevault_auth/install/library.py).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.stores.gamevault.store import GameVaultStore


def _make_store(**auth_overrides) -> GameVaultStore:
    """Build a GameVaultStore with a fake bus/cache and no real config,
    then monkeypatch its internal collaborators with mocks."""
    bus = MagicMock()
    # The store awaits bus.emit (StoreBase._emit), so the bus needs an
    # awaitable emit even in the tests that do not assert on it.
    bus.emit = AsyncMock()
    cache = MagicMock()
    store = GameVaultStore(bus, cache, plugin_dir=None, config=None)

    store._auth = MagicMock()
    store._auth.is_authenticated.return_value = auth_overrides.get("is_authenticated", True)
    store._auth.get_auth_headers = AsyncMock(
        return_value=auth_overrides.get("auth_headers", {"Authorization": "Bearer x"}),
    )
    store._auth.server_url = auth_overrides.get("server_url", "https://gv.example.com")
    store._auth.verify_ssl = auth_overrides.get("verify_ssl", True)
    store._auth.download_dir = auth_overrides.get("download_dir", None)

    store._installer = MagicMock()
    store._library_reader = MagicMock()
    return store


# ── is_available ────────────────────────────────────────────────────────
async def test_is_available_reflects_auth_state():
    store = _make_store(is_authenticated=True)
    assert await store.is_available() is True

    store2 = _make_store(is_authenticated=False)
    assert await store2.is_available() is False


# ── start_auth / complete_auth ────────────────────────────────────────
async def test_start_auth_forwards_kwargs_to_auth_module():
    store = _make_store()
    store._auth.start_auth = AsyncMock(return_value=MagicMock(success=True))

    await store.start_auth(
        server_url="https://gv.example.com",
        username="alice",
        password="secret",
        verify_ssl=False,
        download_dir="/mnt/dl",
    )

    store._auth.start_auth.assert_awaited_once_with(
        server_url="https://gv.example.com",
        username="alice",
        password="secret",
        verify_ssl=False,
        download_dir="/mnt/dl",
    )


async def test_start_auth_defaults_missing_kwargs():
    store = _make_store()
    store._auth.start_auth = AsyncMock(return_value=MagicMock(success=True))

    await store.start_auth()

    store._auth.start_auth.assert_awaited_once_with(
        server_url="", username="", password="", verify_ssl=True, download_dir=None,
    )


async def test_complete_auth_success_when_authenticated():
    store = _make_store(is_authenticated=True)
    result = await store.complete_auth()
    assert result.success is True
    assert result.action == "authenticated"


async def test_complete_auth_failure_when_not_authenticated():
    store = _make_store(is_authenticated=False)
    result = await store.complete_auth()
    assert result.success is False


# ── logout ────────────────────────────────────────────────────────────
async def test_logout_delegates_to_auth():
    store = _make_store()
    store._auth.logout = AsyncMock(return_value=MagicMock(success=True))
    result = await store.logout()
    store._auth.logout.assert_awaited_once()
    assert result.success is True


# ── get_library ───────────────────────────────────────────────────────
async def test_get_library_returns_none_when_not_authenticated():
    store = _make_store()
    store._auth.get_auth_headers = AsyncMock(return_value=None)
    assert await store.get_library() is None


async def test_get_library_returns_games_on_success():
    store = _make_store()
    fake_games = [MagicMock()]
    store._library_reader.get_library = AsyncMock(return_value=fake_games)

    result = await store.get_library()

    assert result is fake_games
    store._library_reader.get_library.assert_awaited_once()


async def test_get_library_returns_none_on_exception():
    store = _make_store()
    store._library_reader.get_library = AsyncMock(side_effect=RuntimeError("boom"))

    result = await store.get_library()

    assert result is None


# ── install_game ──────────────────────────────────────────────────────
async def test_install_game_forwards_to_installer():
    """The store is a pass-through now; the transport is the source's job.

    ``server_url``, ``download_dir`` and the auth check used to be threaded
    through this call. They moved into ``RemoteArchiveSource`` when the two
    modes were folded onto one pipeline — see
    ``test_gamevault_sources.py`` — so that install, uninstall, size and
    library have no mode branch left to get wrong.
    """
    store = _make_store()
    store._installer.install_game = AsyncMock(return_value=MagicMock(success=True))

    await store.install_game("123", base_path="/games", progress_cb=None)

    store._installer.install_game.assert_awaited_once()
    args, kwargs = store._installer.install_game.call_args
    assert args == ("123",)
    assert kwargs["install_path"] == "/games"
    assert set(kwargs) == {"install_path", "progress_callback"}


# ── uninstall_game / update_game / check_for_updates ───────────────────
async def test_uninstall_game_delegates_to_installer():
    store = _make_store()
    store._installer.uninstall_game = AsyncMock(return_value=MagicMock(success=True))

    await store.uninstall_game("123")

    store._installer.uninstall_game.assert_awaited_once_with("123")


async def test_uninstall_game_emits_game_uninstalled():
    """The shortcut flips to not-installed via the event, not a direct call.

    ``ShortcutService`` subscribes to ``GAME_UNINSTALLED`` and calls
    ``mark_uninstalled`` itself, which is how the other five stores do it.
    A store that stays silent here leaves its shortcut reading "Installed"
    forever — and the alternative fix (calling ``mark_uninstalled`` from the
    uninstall RPC) would fire twice for every store that does emit.
    """
    from unifideck.core.types import Events

    store = _make_store()
    store._bus.emit = AsyncMock()
    store._installer.uninstall_game = AsyncMock(return_value=MagicMock(success=True))

    await store.uninstall_game("123")

    store._bus.emit.assert_awaited_once_with(
        Events.GAME_UNINSTALLED, store="gamevault", game_id="123",
    )


async def test_uninstall_game_stays_silent_when_the_removal_failed():
    store = _make_store()
    store._bus.emit = AsyncMock()
    store._installer.uninstall_game = AsyncMock(return_value=MagicMock(success=False))

    await store.uninstall_game("123")

    store._bus.emit.assert_not_awaited()


async def test_update_game_reinstalls():
    store = _make_store()
    store._installer.install_game = AsyncMock(return_value=MagicMock(success=True))

    await store.update_game("123")

    store._installer.install_game.assert_awaited_once()


async def test_check_for_updates_always_empty():
    store = _make_store()
    assert await store.check_for_updates() == []


# ── get_game_size ─────────────────────────────────────────────────────
async def test_get_game_size_forwards_to_installer():
    store = _make_store()
    store._installer.get_game_size = AsyncMock(return_value=1234)

    result = await store.get_game_size("123")

    assert result == 1234
    store._installer.get_game_size.assert_awaited_once()


# ── backward-compat helpers ───────────────────────────────────────────
def test_get_install_info_delegates_to_installer():
    store = _make_store()
    store._installer.get_install_info.return_value = {"title": "T"}
    assert store._get_install_info("123") == {"title": "T"}


async def test_get_installed_delegates_to_installer():
    store = _make_store()
    store._installer.get_installed.return_value = {"1": {"title": "A"}}
    result = await store.get_installed()
    assert result == {"1": {"title": "A"}}


# ── store_info sanity ─────────────────────────────────────────────────
def test_store_info_declares_manual_auth_and_install_support():
    assert GameVaultStore.store_info.name == "gamevault"
    assert GameVaultStore.store_info.auth_method == "manual"
    assert GameVaultStore.store_info.supports_install is True


def test_store_info_declares_no_derived_capability():
    """The capability fields are derived, and declaring one must stay fatal.

    ``uses_wine`` and ``supports_cloud_saves`` were removed from StoreInfo
    (audit §3.1, register 26/31) because a per-store literal is a second
    copy that drifts — ``supports_cloud_saves`` was not merely unread but
    wrong, with both cloud-save stores advertising none. They are derived in
    ``get_store_infos`` now, and re-adding one raises TypeError. This asserts
    the absence, not the value: an assertion on ``is False`` would pass again
    the moment somebody re-added the field.
    """
    assert not hasattr(GameVaultStore.store_info, "uses_wine")
    assert not hasattr(GameVaultStore.store_info, "supports_cloud_saves")

    from unifideck.core.store_capabilities import capability_flags

    # GameVault is in none of the capability sets.
    assert capability_flags("gamevault") == {
        "supports_achievements": False,
        "supports_cloud_saves": False,
        "has_language_picker": False,
        "has_browser_storefront": False,
    }
