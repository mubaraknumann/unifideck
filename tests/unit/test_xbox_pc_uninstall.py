"""Xbox PC uninstall: never gated, never a phantom success, never under a lock.

Uninstall works with the feature off, the components missing and the user
signed out: the uninstaller is built with the event bus alone. It deletes
the whole install root, the launch views next to ``game/`` included.
"""
from __future__ import annotations

import fcntl
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from _xbox_pc_fakes import CID, PID, FakeBus, write_game

from unifideck.core import marker_sweep
from unifideck.core.manifest import write_manifest
from unifideck.core.types import Events
from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.install_records import read_install_record, write_install_record
from unifideck.stores.microsoft.xodus.uninstaller import XboxPcUninstaller


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    games_map = tmp_path / "games.map"
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path",
                        lambda config=None: str(games_map))
    roots: set[Path] = set()
    monkeypatch.setattr(marker_sweep, "collect_install_roots", lambda: set(roots))
    root = tmp_path / "games" / "Xbox" / PID
    return {"tmp": tmp_path, "root": root, "roots": roots, "games_map": games_map}


async def _install(root: Path, *, record: bool = True, marker: bool = True) -> None:
    write_game(root / "game")
    (root / ".xodus-chain-1a2b").mkdir()
    (root / ".xodus-chain-1a2b" / "love.exe").write_bytes(b"MZ")
    (root / ".xodus-offline-3c").mkdir()
    if marker:
        assert await write_manifest(install_dir=str(root), store="microsoft", store_id=PID,
                                    title="Balatro", executable_relative="game/love.exe")
    if record:
        write_install_record({
            "schema": 1, "productId": PID, "state": "installed", "market": "IN",
            "contentId": CID, "installRoot": str(root), "gameDir": str(root / "game"),
            "exe": str(root / "game" / "love.exe"), "exeRelative": "love.exe",
            "version": "1.0.5.0", "titleId": "6adc6be5", "startedAt": 1.0,
            "installedAt": 2.0,
        })


def _uninstalled(bus: FakeBus) -> list[dict[str, Any]]:
    return [kw for ev, kw in bus.events if ev == Events.GAME_UNINSTALLED]


async def test_uninstall_removes_the_root_and_its_launch_views(env: dict[str, Any]) -> None:
    await _install(env["root"])
    bus = FakeBus()
    result = await XboxPcUninstaller(bus).uninstall(PID)
    assert result.success
    assert not env["root"].exists()
    assert read_install_record(PID) is None
    assert _uninstalled(bus) == [{"store": "microsoft", "game_id": PID}]


async def test_a_lost_record_falls_back_to_the_marker(env: dict[str, Any]) -> None:
    await _install(env["root"], record=False)
    env["roots"].add(env["root"].parent)
    result = await XboxPcUninstaller(FakeBus()).uninstall(PID)
    assert result.success and not env["root"].exists()


async def test_a_stream_only_title_is_not_supported(env: dict[str, Any]) -> None:
    bus = FakeBus()
    result = await XboxPcUninstaller(bus).uninstall(PID)
    assert not result.success and result.error_code == "not_supported"
    assert _uninstalled(bus) == []


async def test_a_bad_id_is_not_supported(env: dict[str, Any]) -> None:
    result = await XboxPcUninstaller(FakeBus()).uninstall("../../etc")
    assert result.error_code == "not_supported"


async def test_a_row_id_that_differs_from_the_product_id(env: dict[str, Any]) -> None:
    # Installed under another store_game_id: the record carries both ids.
    await _install(env["root"], marker=False)
    record = read_install_record(PID)
    assert record is not None
    write_install_record(dict(record, storeGameId="xcloud-balatro"))
    bus = FakeBus()
    assert (await XboxPcUninstaller(bus).uninstall("xcloud-balatro")).success
    assert not env["root"].exists()
    assert _uninstalled(bus) == [{"store": "microsoft", "game_id": "xcloud-balatro"}]


async def test_a_record_whose_folder_is_gone_is_forgotten(env: dict[str, Any]) -> None:
    await _install(env["root"])
    shutil.rmtree(env["root"])
    bus = FakeBus()
    assert (await XboxPcUninstaller(bus).uninstall(PID)).success
    assert read_install_record(PID) is None and len(_uninstalled(bus)) == 1


async def test_a_record_on_a_drive_that_is_not_connected_is_kept(env: dict[str, Any]) -> None:
    """An unmounted SD card looks like a missing folder. The record must stay."""
    await _install(env["root"])
    shutil.rmtree(env["tmp"] / "games")  # the whole <base>: the card is away
    bus = FakeBus()
    result = await XboxPcUninstaller(bus).uninstall(PID)
    assert not result.success and result.error_code == "xbox_install_unreachable"
    assert str(env["root"]) in str(result.error)
    assert read_install_record(PID) is not None and _uninstalled(bus) == []


async def test_refused_while_the_game_runs(env: dict[str, Any]) -> None:
    await _install(env["root"])
    bus = FakeBus()
    with game_lock(PID) as held:
        assert held
        result = await XboxPcUninstaller(bus).uninstall(PID)
    assert result.error_code == "xbox_game_running"
    assert env["root"].exists() and read_install_record(PID) is not None
    assert _uninstalled(bus) == []


async def test_refused_while_a_download_writes_the_folder(env: dict[str, Any]) -> None:
    await _install(env["root"])
    fd = os.open(env["root"] / "game" / ".nexus-streaming.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        result = await XboxPcUninstaller(FakeBus()).uninstall(PID)
    finally:
        os.close(fd)
    assert result.error_code == "xbox_install_in_progress" and env["root"].exists()


async def test_refused_when_another_store_shares_the_folder(env: dict[str, Any]) -> None:
    await _install(env["root"])
    env["games_map"].write_text(f"gog:1=\t{env['root']}/game/other.exe\t{env['root']}\t1\n")
    result = await XboxPcUninstaller(FakeBus()).uninstall(PID)
    assert result.error_code == "uninstall_refused_unsafe_path" and env["root"].exists()


async def test_a_record_outside_the_layout_is_not_trusted(env: dict[str, Any]) -> None:
    odd = env["tmp"] / "games" / "NotXbox" / PID
    await _install(odd, marker=False)
    result = await XboxPcUninstaller(FakeBus()).uninstall(PID)
    # The record is forgotten; a folder that is not ``Xbox/<PID>`` is never deleted.
    assert result.success and odd.exists()


async def test_the_marker_sweep_removes_the_whole_root(env: dict[str, Any]) -> None:
    await _install(env["root"])
    assert marker_sweep.sweep_all({env["root"].parent}) == 1
    assert not env["root"].exists()


async def test_store_hooks_uninstall_needs_nothing_but_the_bus(env: dict[str, Any]) -> None:
    from unifideck.stores.microsoft.xodus.store_hooks import XboxPcStoreHooks

    await _install(env["root"])

    class _Unusable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"uninstall touched {name}")

    hooks = XboxPcStoreHooks(FakeBus(), _Unusable(), _Unusable(), lambda _pid: None)  # type: ignore[arg-type]
    assert (await hooks.uninstall(PID)).success
    assert not env["root"].exists()
