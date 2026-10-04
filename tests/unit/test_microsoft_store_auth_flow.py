"""MicrosoftStore picks its sign-in flow from config, and the registry can stop it.

- device code: the Edge profile's Microsoft cookies are kept. Xbox Cloud
  Gaming streams on them, and the device page reuses the session;
- browser redirect (the legacy flow): cookies are cleared first, as before,
  so the login form appears and its redirect can be captured;
- ``cancel`` reaches the store (a no-op for stores without a running poll).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.types import AuthResult, Result
from unifideck.stores.microsoft import microsoft_store as ms_module
from unifideck.stores.microsoft.microsoft_config import (
    AUTH_FLOW_BROWSER_REDIRECT,
    AUTH_FLOW_DEVICE_CODE,
    MicrosoftConfig,
)
from unifideck.stores.microsoft.microsoft_store import MicrosoftStore
from unifideck.stores.shared.store_registry import StoreRegistry


def _store(flow: str) -> tuple[MicrosoftStore, MagicMock]:
    store = MicrosoftStore.__new__(MicrosoftStore)
    store._ms_config = MicrosoftConfig(auth_flow=flow)
    edge = MagicMock()
    edge.is_installed = True
    store._edge = edge
    store._device_auth = MagicMock()
    store._device_auth.start_auth = AsyncMock(return_value=AuthResult(success=True, store="microsoft"))
    store._device_auth.cancel = AsyncMock()
    store._auth = MagicMock()
    store._auth.start_auth = AsyncMock(return_value=AuthResult(success=True, store="microsoft"))
    store._tokens = MagicMock(client_mismatch=False)
    return store, edge


@pytest.fixture(autouse=True)
def _no_controller_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ms_module.EdgeBrowser, "ensure_controller_permissions", staticmethod(lambda: True))


async def test_device_code_keeps_the_cookies() -> None:
    store, edge = _store(AUTH_FLOW_DEVICE_CODE)
    await store.start_auth()
    store._device_auth.start_auth.assert_awaited_once()
    store._auth.start_auth.assert_not_awaited()
    edge.clear_store_cookies.assert_not_called()


async def test_browser_redirect_clears_them_as_before() -> None:
    store, edge = _store(AUTH_FLOW_BROWSER_REDIRECT)
    await store.start_auth()
    store._auth.start_auth.assert_awaited_once()
    store._device_auth.start_auth.assert_not_awaited()
    assert {c.args[0] for c in edge.clear_store_cookies.call_args_list} == {"microsoft.com", "live.com"}


async def test_no_edge_means_install_edge_first() -> None:
    store, edge = _store(AUTH_FLOW_DEVICE_CODE)
    edge.is_installed = False
    result = await store.start_auth()
    assert result.error == "edge_not_installed"


async def test_cancel_reaches_the_device_flow() -> None:
    store, _ = _store(AUTH_FLOW_DEVICE_CODE)
    assert (await store.cancel_auth()).success
    store._device_auth.cancel.assert_awaited_once()


# ── the registry ─────────────────────────────────────────────────────

class _Info:
    name = "microsoft"
    display_name = "Microsoft"


class _RegStore:
    store_info = _Info()

    def __init__(self, *, cancellable: bool) -> None:
        self.cancelled = 0
        if cancellable:
            self.cancel_auth = self._cancel

    async def _cancel(self) -> Result:
        self.cancelled += 1
        return Result(success=True)

    async def is_available(self) -> bool:
        return False


def _registry(store: Any) -> StoreRegistry:
    registry = StoreRegistry.__new__(StoreRegistry)
    registry._stores = {"microsoft": store}
    registry._bus = MagicMock()
    return registry


async def test_registry_cancel_reaches_the_store() -> None:
    store = _RegStore(cancellable=True)
    assert (await _registry(store).auth_action("microsoft", "cancel")).success
    assert store.cancelled == 1


async def test_registry_cancel_is_a_noop_for_other_stores() -> None:
    assert (await _registry(_RegStore(cancellable=False)).auth_action("microsoft", "cancel")).success
