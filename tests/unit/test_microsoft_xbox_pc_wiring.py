"""How ``MicrosoftStore`` wires Xbox PC in, and the uninstall glue around it.

* The xCloud sign-in and the Xbox PC sign-in share the Edge auth window, so
  ``start_auth`` refuses while the Xbox PC one runs, and ``logout`` stops it
  before Edge is killed.
* The store builds one facade, one PC library and one set of install hooks,
  and an explicit Xodus sign-out clears the PC list.
* The uninstall RPC never sweeps a folder the Xbox PC uninstaller refused
  to delete (a running game, a download in progress).
* The prefix of an Xbox PC game is named after its product id, which can
  differ from the row id the central uninstall flow deletes.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.safe_delete import canonical_prefix
from unifideck.core.types import Result
from unifideck.launcher.xbox_pc.install_records import write_install_record
from unifideck.rpc.mixins import download as download_mod
from unifideck.rpc.mixins.download import DownloadRPCMixin
from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
from unifideck.stores.microsoft.microsoft_store import MicrosoftStore
from unifideck.stores.microsoft.xodus.store_hooks import XboxPcStoreHooks

PID = "9PK087LNGJC5"


class _Bus:
    async def emit(self, *_a: Any, **_k: Any) -> None:
        return None

    def on(self, *_a: Any, **_k: Any) -> None:
        return None


class _Cache:
    def get(self, *_a: Any, **_k: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None

    def register(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / ".local" / "share"))
    return root


# ── construction ─────────────────────────────────────────────────────

def test_the_store_builds_one_facade_library_and_hooks(home: Path) -> None:
    store = MicrosoftStore(_Bus(), _Cache(), plugin_dir="/plugin")
    assert store.xbox_pc is store._xbox_pc
    assert store._pc_library._cli is store.xbox_pc.cli  # type: ignore[attr-defined]
    assert store._pc_library.clear in store.xbox_pc.sign_out_listeners
    assert isinstance(store._xbox_hooks, XboxPcStoreHooks)


def test_the_status_reads_the_xcloud_sign_in(home: Path) -> None:
    store = MicrosoftStore(_Bus(), _Cache(), plugin_dir="/plugin")
    store._cached_available = True
    status = asyncio.run(store.xbox_pc.status())
    assert status["xcloud_signed_in"] is True
    assert status["enabled"] is False


# ── the shared Edge window ───────────────────────────────────────────

def _auth_store() -> MicrosoftStore:
    store = MicrosoftStore.__new__(MicrosoftStore)
    store._ms_config = MicrosoftConfig(auth_flow="device_code")
    store._edge = MagicMock(is_installed=True)
    store._device_auth = MagicMock(cancel=AsyncMock(), start_auth=AsyncMock())
    store._auth = None
    store._tokens = MagicMock(clear=AsyncMock())
    store._bus = MagicMock(emit=AsyncMock())
    store._cached_available = True
    store._xbox_pc = MagicMock(on_microsoft_logout=AsyncMock(), sign_in_running=False)
    return store


def test_start_auth_refuses_while_the_xbox_pc_sign_in_runs() -> None:
    store = _auth_store()
    store._xbox_pc.sign_in_running = True
    result = asyncio.run(store.start_auth())
    assert result.success is False and result.error == "auth_in_progress"
    store._device_auth.start_auth.assert_not_called()


def test_logout_stops_the_xbox_pc_sign_in_before_killing_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _auth_store()
    order: list[str] = []
    store._xbox_pc.on_microsoft_logout = AsyncMock(side_effect=lambda: order.append("xbox_pc"))
    store._device_auth.cancel = AsyncMock(side_effect=lambda: order.append("device"))
    store._edge.kill = MagicMock(side_effect=lambda: order.append("edge"))
    store._xbox_pc.notify = MagicMock(side_effect=lambda: order.append("notify"))
    monkeypatch.setattr("unifideck.stores.microsoft.microsoft_store.EdgeBrowser.clear_profile_data", lambda: None)
    monkeypatch.setattr("unifideck.stores.microsoft.microsoft_store.remove_url_file", AsyncMock())
    assert asyncio.run(store.logout()).success
    assert order == ["xbox_pc", "device", "edge", "notify"]
    assert store._cached_available is False


# ── the uninstall RPC ────────────────────────────────────────────────

class _Store:
    def __init__(self, result: Result) -> None:
        self.result = result

    async def uninstall_game(self, game_id: str, **_kw: Any) -> Result:
        return self.result


def _host(monkeypatch: pytest.MonkeyPatch, store_name: str, result: Result) -> list[tuple[str, str]]:
    swept: list[tuple[str, str]] = []
    monkeypatch.setattr(download_mod.marker_sweep, "sweep_game", lambda s, g: swept.append((s, g)))
    host = DownloadRPCMixin.__new__(DownloadRPCMixin)
    host.registry = SimpleNamespace(get_store=lambda _name: _Store(result))  # type: ignore[attr-defined]
    host.sync_service = SimpleNamespace(  # type: ignore[attr-defined]
        get_game_info=lambda _app_id: {"store": store_name, "store_game_id": PID},
    )
    asyncio.run(host.uninstall_game(1))
    return swept


def test_a_refused_xbox_pc_uninstall_is_not_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = Result(success=False, error="xbox_game_running", error_code="xbox_game_running")
    assert _host(monkeypatch, "microsoft", refused) == []


def test_a_successful_xbox_pc_uninstall_is_still_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _host(monkeypatch, "microsoft", Result(success=True)) == [("microsoft", PID)]


def test_other_stores_are_swept_even_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOG can fail to find a folder it then leaves behind: the sweep is its net."""
    assert _host(monkeypatch, "gog", Result(success=False)) == [("gog", PID)]


# ── the prefix named after the product id ────────────────────────────

def _record(game_id: str, home: Path) -> None:
    """A record whose folder was deleted by hand (its drive is still there)."""
    base = home / "games"
    base.mkdir(exist_ok=True)
    root = base / "Xbox" / PID
    write_install_record({
        "schema": 1, "productId": PID, "state": "installed", "storeGameId": game_id,
        "installRoot": str(root), "gameDir": str(root / "game"),
    })


def test_uninstall_deletes_the_product_id_prefix(home: Path) -> None:
    _record("xcloud-row", home)
    prefix = canonical_prefix(PID)
    prefix.mkdir(parents=True)
    hooks = XboxPcStoreHooks(_Bus(), MagicMock(), MagicMock(), lambda _g: None)
    assert asyncio.run(hooks.uninstall("xcloud-row", delete_prefix=True)).success
    assert not prefix.exists()


def test_uninstall_keeps_the_prefix_unless_asked(home: Path) -> None:
    _record("xcloud-row", home)
    prefix = canonical_prefix(PID)
    prefix.mkdir(parents=True)
    hooks = XboxPcStoreHooks(_Bus(), MagicMock(), MagicMock(), lambda _g: None)
    assert asyncio.run(hooks.uninstall("xcloud-row")).success
    assert prefix.exists()
