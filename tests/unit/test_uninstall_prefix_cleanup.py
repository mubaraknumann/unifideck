"""Uninstall's "also delete Proton prefix" option, for every store.

Each store used to delete its own prefix, and each one that forgot leaked
it: GOG dropped ``delete_prefix`` via ``**kwargs`` (~1.6 GB), and GameVault
never deleted it at all (found while validating PR #494). The deletion now
lives once in ``DownloadRPCMixin.uninstall_game``, through
``core.safe_delete.delete_game_prefix``, so a new store gets it for free.
Wrapper stores are the exception: the game lives inside their prefix and
each deletes it through its own guard.

Amazon also leaked a stub install dir — ``nile uninstall`` removes only the
files it tracks, leaving our own ``.unifideck_manifest.json`` (and the
directory with it) behind. That store-level fix is covered at the end.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.safe_delete import canonical_prefix, delete_game_prefix
from unifideck.core.types import Result
from unifideck.rpc.mixins import download as download_mod
from unifideck.rpc.mixins.download import DownloadRPCMixin
from unifideck.stores.amazon.amazon_install import AmazonInstaller

_STORES = ["epic", "gog", "amazon", "itch", "gamevault"]
_WRAPPER_STORES = ["battlenet", "ubisoft"]


def _fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


class _Store:
    def __init__(self, success: bool) -> None:
        self.success = success
        self.kwargs: dict[str, Any] = {}

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        self.kwargs = kwargs
        return Result(success=self.success)


def _host(
    monkeypatch: pytest.MonkeyPatch, store_name: str, store: _Store, game_id: str,
) -> DownloadRPCMixin:
    monkeypatch.setattr(download_mod.marker_sweep, "sweep_game", lambda *_a: None)
    host = DownloadRPCMixin.__new__(DownloadRPCMixin)
    host.registry = SimpleNamespace(get_store=lambda _name: store)  # type: ignore[attr-defined]
    host.sync_service = SimpleNamespace(  # type: ignore[attr-defined]
        get_game_info=lambda _app_id: {"store": store_name, "store_game_id": game_id},
    )
    return host


@pytest.mark.parametrize("store_name", _STORES)
async def test_uninstall_deletes_prefix_when_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_name: str,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    prefix = canonical_prefix("g1")
    prefix.mkdir(parents=True)
    host = _host(monkeypatch, store_name, _Store(success=True), "g1")

    result = await host.uninstall_game(123, True)

    assert result.success
    assert not prefix.exists()


@pytest.mark.parametrize("store_name", _STORES)
async def test_uninstall_keeps_prefix_when_not_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_name: str,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    prefix = canonical_prefix("g1")
    prefix.mkdir(parents=True)
    host = _host(monkeypatch, store_name, _Store(success=True), "g1")

    assert (await host.uninstall_game(123, False)).success
    assert prefix.exists()


@pytest.mark.parametrize("store_name", _STORES)
async def test_failed_uninstall_keeps_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_name: str,
) -> None:
    """Files still on disk means the game still launches from that prefix."""
    _fake_home(monkeypatch, tmp_path)
    prefix = canonical_prefix("g1")
    prefix.mkdir(parents=True)
    host = _host(monkeypatch, store_name, _Store(success=False), "g1")

    assert not (await host.uninstall_game(123, True)).success
    assert prefix.exists()


@pytest.mark.parametrize("store_name", _WRAPPER_STORES)
async def test_wrapper_stores_delete_their_own_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_name: str,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    prefix = canonical_prefix("g1")
    prefix.mkdir(parents=True)
    store = _Store(success=True)
    host = _host(monkeypatch, store_name, store, "g1")

    assert (await host.uninstall_game(123, True)).success
    assert prefix.exists()
    assert store.kwargs == {"delete_prefix": True}


@pytest.mark.parametrize("game_id", ["", "."])
def test_delete_game_prefix_never_removes_the_prefixes_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, game_id: str,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    sibling = canonical_prefix("other")
    sibling.mkdir(parents=True)

    assert delete_game_prefix(game_id) is False
    assert sibling.exists()


def test_delete_game_prefix_treats_a_missing_prefix_as_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    assert delete_game_prefix("never-launched") is True


# --------------------------------------------------------------------------
# Amazon stub install dir
# --------------------------------------------------------------------------
def _amazon_installer(
    tmp_path: Path, install_dir: Path, *, cli: str | None = None,
) -> AmazonInstaller:
    inst = AmazonInstaller.__new__(AmazonInstaller)
    inst._cli_path = cli
    inst._default_install_root = str(tmp_path / "Games")
    inst._uninstall_timeout = 5
    inst._find_exe = lambda _p, _ids=None: None
    inst._library = MagicMock()
    inst._library.read_installed_ids = AsyncMock(
        return_value={"g1": {"path": str(install_dir)}},
    )
    inst._library.read_owned_games = AsyncMock(return_value=[])
    inst._bus = MagicMock()
    inst._bus.emit = AsyncMock()
    return inst


async def test_amazon_uninstall_removes_stub_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """nile gone/failed → fall back to deleting the dir ourselves."""
    _fake_home(monkeypatch, tmp_path)
    install_dir = tmp_path / "Games" / "Clouds & Sheep 2"
    install_dir.mkdir(parents=True)
    # The exact stub the real bug left behind: only our marker remains.
    (install_dir / ".unifideck_manifest.json").write_text("{}")

    inst = _amazon_installer(tmp_path, install_dir, cli=None)
    result = await inst.uninstall_game("g1")

    assert result.success
    assert not install_dir.exists()
    inst._bus.emit.assert_awaited_once()
