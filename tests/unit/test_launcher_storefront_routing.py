"""``LauncherService._handle_auth_path`` — which handler a shop press hits.

The ordering inside that method is the whole correctness argument, and
nothing about the code's shape makes it obvious: the wrapper check must
come BEFORE the action check.

A wrapper store (Ubisoft, Battle.net) signs in inside a Wine prefix and
has no browser cookies at all, so its signed-in shop is the vendor
client's own Store/Shop tab. Testing ``action == "storefront"`` first
would send a Ubisoft cart press to Edge, which would load
store.ubisoft.com logged out — a plausible-looking window that quietly
fails the one requirement the feature exists for.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.launcher.proton.handlers.battlenet import (
    battlenet_auth_launch,
    battlenet_install_launch,
)
from unifideck.launcher.proton.handlers.ubisoft import (
    ubisoft_auth_launch,
    ubisoft_install_launch,
)
from unifideck.services.launcher.service import LauncherService


class _Ctx:
    """Minimal stand-in for a non-launch ``LaunchContext``."""

    def __init__(self, store: str, action: str) -> None:
        self.store = store
        self.auth_store = store
        self.action = action
        self.is_launch_action = False


class _Service:
    """``LauncherService`` reduced to the routing method under test."""

    def __init__(self) -> None:
        self._edge_browser = object()
        self.wrapper_calls: list[Any] = []

    async def _launch_wrapper_client(self, ctx: Any) -> str:
        self.wrapper_calls.append(ctx)
        return "wrapper"

    _handle_auth_path = LauncherService._handle_auth_path


@pytest.fixture
def spy(monkeypatch):
    """Record which flow module the router reached."""
    seen: list[str] = []

    async def _storefront(_ctx: Any, _edge: Any) -> str:
        seen.append("storefront")
        return "storefront"

    async def _auth(_ctx: Any, _edge: Any) -> str:
        seen.append("auth")
        return "auth"

    import unifideck.launcher.flows.auth as auth_mod
    import unifideck.launcher.flows.storefront as sf_mod

    monkeypatch.setattr(sf_mod, "handle_store_storefront", _storefront)
    monkeypatch.setattr(auth_mod, "handle_store_auth", _auth)
    return seen


# ── Browser stores ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["epic", "gog", "amazon", "microsoft"])
async def test_a_shop_press_reaches_the_storefront_flow(spy, store: str) -> None:
    svc = _Service()

    await svc._handle_auth_path(_Ctx(store, "storefront"))

    assert spy == ["storefront"]
    assert svc.wrapper_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["epic", "gog", "amazon", "microsoft"])
async def test_sign_in_still_reaches_the_auth_flow(spy, store: str) -> None:
    svc = _Service()

    await svc._handle_auth_path(_Ctx(store, "auth"))

    assert spy == ["auth"]


# ── Wrapper stores: the ordering guard ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["ubisoft", "battlenet"])
async def test_a_wrapper_shop_press_opens_the_vendor_client(
    spy, store: str,
) -> None:
    """Not the browser. This is the assertion that pins the check order."""
    svc = _Service()

    await svc._handle_auth_path(_Ctx(store, "storefront"))

    assert len(svc.wrapper_calls) == 1
    assert spy == [], "a wrapper store must never reach a browser flow"


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["ubisoft", "battlenet"])
async def test_a_wrapper_sign_in_still_opens_the_vendor_client(
    spy, store: str,
) -> None:
    svc = _Service()

    await svc._handle_auth_path(_Ctx(store, "auth"))

    assert len(svc.wrapper_calls) == 1
    assert spy == []


# ── Wrapper handler selection ───────────────────────────────────────


@pytest.mark.parametrize(
    ("store", "expected"),
    [
        ("ubisoft", ubisoft_auth_launch),
        ("battlenet", battlenet_auth_launch),
    ],
)
def test_storefront_opens_the_client_bare_like_sign_in(store, expected) -> None:
    """Both actions want the client in the AUTH prefix — where the session is.

    ``storefront`` falls into the same branch as ``auth`` on purpose; it
    is not a missing case.
    """
    assert LauncherService._wrapper_handler(store, "storefront") is expected


@pytest.mark.parametrize(
    ("store", "expected"),
    [
        ("ubisoft", ubisoft_install_launch),
        ("battlenet", battlenet_install_launch),
    ],
)
def test_install_still_gets_its_own_handler(store, expected) -> None:
    assert LauncherService._wrapper_handler(store, "install") is expected


def test_an_unknown_store_has_no_wrapper_handler() -> None:
    assert LauncherService._wrapper_handler("epic", "storefront") is None
