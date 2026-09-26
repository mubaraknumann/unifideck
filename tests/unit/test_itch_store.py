"""Tests for ``stores.itch.store.ItchStore``: orchestration and contracts."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from unifideck.core.types import StoreInfo
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


async def test_uninstall_announces_it_and_honours_delete_prefix(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    # The first device run: files gone, but the shortcut still said Play and
    # the games.map row and Proton prefix stayed, because nothing emitted
    # GAME_UNINSTALLED (the event ShortcutService acts on).
    from unifideck.core.types import Events, Result
    from unifideck.stores.itch import store as store_mod

    store = _store(tmp_path)
    store._installer.uninstall = AsyncMock(return_value=Result(success=True, store="itch"))  # type: ignore[method-assign]
    removed: list[Path] = []
    monkeypatch.setattr(store_mod, "canonical_prefix", lambda gid: tmp_path / "prefixes" / gid)
    monkeypatch.setattr(store_mod, "safe_rmtree", lambda p: removed.append(Path(p)) or True)
    result = await store.uninstall_game("1193828", delete_prefix=True)
    assert result.success
    assert removed == [tmp_path / "prefixes" / "1193828"]
    store._bus.emit.assert_awaited_with(Events.GAME_UNINSTALLED, store="itch", game_id="1193828")


async def test_failed_uninstall_announces_nothing(tmp_path: Path) -> None:
    from unifideck.core.types import Result

    store = _store(tmp_path)
    store._installer.uninstall = AsyncMock(return_value=Result(success=False, store="itch", error="x"))  # type: ignore[method-assign]
    assert not (await store.uninstall_game("1", delete_prefix=True)).success
    store._bus.emit.assert_not_awaited()
