"""Tests for ``stores.itch.store.ItchStore``: orchestration and contracts."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from unifideck.core.types import Events, StoreInfo
from unifideck.stores.itch.butlerd import ButlerdError
from unifideck.stores.itch.store import ItchStore


class _Cfg:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _store(tmp_path: Path) -> ItchStore:
    bus = MagicMock()
    bus.emit = AsyncMock()
    cfg = _Cfg({"stores.itch": {"butler_db": str(tmp_path / "butler" / "butler.db"),
                                "default_install_root": str(tmp_path / "games")}})
    return ItchStore(bus, MagicMock(), plugin_dir=str(tmp_path), config=cfg)


def test_store_info_is_pinned() -> None:
    # ``name`` must equal the directory (validate_architecture check 3) and
    # the unifiDB ``external_ids`` key, which the catalog generator emits as
    # "itch" for IGDB category 30.
    assert ItchStore.store_info == StoreInfo(
        name="itch", display_name="itch.io", auth_method="oauth",
        icon_asset="itch.png", supports_install=True,
    )
    assert ItchStore.CLI_TOOL.search_paths == ["bin/butler/butler"]


async def test_no_database_means_signed_out_without_spawning_butler(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.cli_path = "/bin/true"
    store._daemon._cli_path = "/bin/true"
    store._daemon.call = AsyncMock()  # type: ignore[method-assign]
    assert await store.is_available() is False
    store._daemon.call.assert_not_called()


async def test_is_available_asks_butler_for_a_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.cli_path = "/bin/true"
    (tmp_path / "butler").mkdir()
    (tmp_path / "butler" / "butler.db").write_text("")
    store._library.profile_id = AsyncMock(return_value=7)  # type: ignore[method-assign]
    assert await store.is_available() is True
    assert store._cached_available is True


async def test_is_available_survives_a_dead_daemon(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.cli_path = "/bin/true"
    (tmp_path / "butler").mkdir()
    (tmp_path / "butler" / "butler.db").write_text("")
    store._library.profile_id = AsyncMock(side_effect=ButlerdError("lost"))  # type: ignore[method-assign]
    assert await store.is_available() is False


async def test_library_without_butler_is_unreadable_not_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.cli_path = None
    assert await store.get_library() is None


async def test_a_refused_key_toasts_once_per_streak_and_keeps_shortcuts(tmp_path: Path) -> None:
    # Rule 8: a 403 costs the whole itch.io library, so it is not log-only.
    store = _store(tmp_path)
    store.cli_path = "/bin/true"

    async def refused() -> None:
        store._library.key_rejected = True

    async def readable() -> list[Any]:
        store._library.key_rejected = False
        return []

    store._library.get_library = AsyncMock(side_effect=refused)  # type: ignore[method-assign]
    assert await store.get_library() is None
    assert await store.get_library() is None
    toasts = [c for c in store._bus.emit.await_args_list if c.args[0] == Events.LAUNCHER_STAGE]
    assert len(toasts) == 1
    assert toasts[0].kwargs["i18n_key"] == "toasts.itchKeyRejectedMessage"
    store._library.get_library = AsyncMock(side_effect=readable)  # type: ignore[method-assign]
    assert await store.get_library() == []
    store._library.get_library = AsyncMock(side_effect=refused)  # type: ignore[method-assign]
    await store.get_library()
    assert store._bus.emit.await_count == 2


async def test_start_auth_needs_edge(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._auth = MagicMock()
    store._edge = MagicMock(is_installed=False)
    result = await store.start_auth()
    assert not result.success and result.error == "edge_not_installed"


async def test_find_installed_exe_uses_the_cave_upload_platform(tmp_path: Path) -> None:
    store = _store(tmp_path)
    folder = tmp_path / "games" / "hampton-court"
    (folder / "HamptonCourt-1.0-pc").mkdir(parents=True)
    (folder / "HamptonCourt-1.0-pc" / "HamptonCourt.exe").write_bytes(b"MZ" + b"\0" * 64)
    store._library.installed_map = AsyncMock(return_value={  # type: ignore[method-assign]
        "354683": {"install_path": str(folder), "cave_id": "c",
                   "upload": {"platforms": {"windows": "all"}}, "title": "Hampton Court"},
    })
    exe = await store.find_installed_exe(str(folder), "354683")
    assert exe == str(folder / "HamptonCourt-1.0-pc" / "HamptonCourt.exe")


async def test_shutdown_stops_the_daemon(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._daemon.shutdown = AsyncMock()  # type: ignore[method-assign]
    await store.shutdown()
    store._daemon.shutdown.assert_awaited_once()


async def test_uninstall_announces_it(tmp_path: Path) -> None:
    # The first device run: files gone, but the shortcut still said Play and
    # the games.map row stayed, because nothing emitted GAME_UNINSTALLED (the
    # event ShortcutService acts on). The prefix is the uninstall RPC's job,
    # covered in test_uninstall_prefix_cleanup.py.
    from unifideck.core.types import Events, Result

    store = _store(tmp_path)
    store._installer.uninstall = AsyncMock(return_value=Result(success=True, store="itch"))  # type: ignore[method-assign]
    result = await store.uninstall_game("1193828", delete_prefix=True)
    assert result.success
    store._bus.emit.assert_awaited_with(Events.GAME_UNINSTALLED, store="itch", game_id="1193828")


async def test_failed_uninstall_announces_nothing(tmp_path: Path) -> None:
    from unifideck.core.types import Result

    store = _store(tmp_path)
    store._installer.uninstall = AsyncMock(return_value=Result(success=False, store="itch", error="x"))  # type: ignore[method-assign]
    assert not (await store.uninstall_game("1", delete_prefix=True)).success
    store._bus.emit.assert_not_awaited()


# ── bundle claim ───────────────────────────────────────────────────────
class _OwnershipDaemon:
    """Answers the reads the claim hooks make; a claim flips ownership."""

    def __init__(self, source: str, *, claims_on_refresh: bool) -> None:
        self.source = source
        self.claims_on_refresh = claims_on_refresh
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        if method == "Profile.List":
            return {"profiles": [{"id": 42}]}
        if method == "Fetch.GameOwnership":
            return {"owned": True, "source": self.source, "bundleId": 520}
        if method == "Fetch.ProfileOwnedBundles":
            return {"items": [{"bundle": {"id": 520, "title": "Racial Justice"}}]}
        if method == "Fetch.ProfileOwnedKeys":
            if self.claims_on_refresh:
                self.source = "download_key"
            return {"items": []}
        raise AssertionError(method)


def _claim_store(tmp_path: Path, monkeypatch: Any, daemon: _OwnershipDaemon) -> ItchStore:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = _store(tmp_path)
    store.cli_path = "/bin/true"
    store._daemon.call = daemon.call  # type: ignore[method-assign]
    return store


async def test_an_unclaimed_bundle_game_is_refused_before_queueing(tmp_path: Path, monkeypatch: Any) -> None:
    store = _claim_store(tmp_path, monkeypatch, _OwnershipDaemon("bundle", claims_on_refresh=False))
    assert (await store.install_precondition("2") or "").startswith("itch_claim_required:")
    store = _claim_store(tmp_path, monkeypatch, _OwnershipDaemon("download_key", claims_on_refresh=False))
    assert await store.install_precondition("2") is None


async def test_begin_claim_points_the_next_shop_window_at_the_bundles_page(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    from unifideck.core.store_urls import take_storefront_target

    store = _claim_store(tmp_path, monkeypatch, _OwnershipDaemon("bundle", claims_on_refresh=False))
    info = await store.begin_claim("2")
    assert info == {"url": "https://itch.io/my-purchases/bundles", "bundle_title": "Racial Justice"}
    assert take_storefront_target("itch") == "https://itch.io/my-purchases/bundles"
    assert take_storefront_target("itch") == ""  # one-time


async def test_finish_claim_rereads_the_owned_keys(tmp_path: Path, monkeypatch: Any) -> None:
    daemon = _OwnershipDaemon("bundle", claims_on_refresh=True)
    store = _claim_store(tmp_path, monkeypatch, daemon)
    assert await store.finish_claim("2") is True
    refresh = next(p for m, p in daemon.calls if m == "Fetch.ProfileOwnedKeys")
    assert refresh["fresh"] is True and refresh["profileId"] == 42
    # Closed without claiming: still a bundle game.
    store = _claim_store(tmp_path, monkeypatch, _OwnershipDaemon("bundle", claims_on_refresh=False))
    assert await store.finish_claim("2") is False
