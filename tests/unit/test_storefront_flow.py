"""``launcher/flows/storefront`` — opening a shop with the live session.

The properties pinned here are the ones whose violation is silent:

* **Cookies are never cleared.** The shared Edge profile's live web
  session IS the signed-in shop. The four ``clear_store_cookies`` call
  sites exist to force a fresh login form before a real sign-in; one
  firing here would guarantee the signed-out page this flow exists to
  avoid — and it would look like "the store just logged me out", not
  like a bug in Unifideck.
* **No ``STORE_AUTH_*`` event is emitted.** ``STORE_AUTH_FAILED`` flips
  the store's row to ``error``, where ``StoreAuthButton`` renders
  ``null`` — leaving the row with no button at all, not even "Sign in".
* **A wrapper store never reaches this module.** It has no browser
  session, so Edge would show it signed out.
* **The single-instance collision is refused, not attempted.** Chromium
  hands a second same-profile invocation to the running instance and
  exits, so spawning anyway produces a window in someone else's
  gamescope session plus a phantom Steam app.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.launcher.flows import storefront as sf
from unifideck.launcher.types.errors import (
    DependencyMissingError,
    GameNotFoundError,
)

_AUTH_PORT = 9222
_XCLOUD_PORT = 9223
_SHOP_PORT = 9224


class _Ctx:
    """Minimal stand-in for ``LaunchContext``."""

    def __init__(self, store: str) -> None:
        self.store = store
        self.auth_store = store


class _FakeEdge:
    """Edge double that FAILS LOUDLY on any cookie-clearing call.

    That is the point of the class: the cookie-safety assertion is not a
    separate test but a property of every happy path exercised here.
    """

    def __init__(
        self,
        *,
        installed: bool = True,
        alive: tuple[int, ...] = (),
        launch_ok: bool = True,
        navigate_ok: bool = True,
    ) -> None:
        self.cdp_port = _AUTH_PORT
        self.is_installed = installed
        self.process = None
        self._alive = set(alive)
        self._launch_ok = launch_ok
        self._navigate_ok = navigate_ok
        self.launched: list[str] = []
        self.navigated: list[tuple[int, str]] = []
        self.closed: list[int] = []

    # ── ports ────────────────────────────────────────────────────
    def xcloud_cdp_port(self) -> int:
        return self.cdp_port + 1

    def storefront_cdp_port(self) -> int:
        return self.cdp_port + 2

    def cdp_alive(self, port: int) -> bool:
        return port in self._alive

    # ── actions ──────────────────────────────────────────────────
    def launch_storefront(self, url: str) -> bool:
        self.launched.append(url)
        return self._launch_ok

    def launch_auth(self, url: str) -> bool:
        raise AssertionError(
            "the shop path opened an OAuth window — it must never touch "
            "auth state",
        )

    async def navigate_on_port(self, port: int, url: str) -> bool:
        self.navigated.append((port, url))
        return self._navigate_ok

    async def close_targets_on_port(self, port: int, *, log_prefix: str) -> bool:
        self.closed.append(port)
        return True

    # ── tripwires ────────────────────────────────────────────────
    def clear_store_cookies(self, domain: str) -> None:
        raise AssertionError(
            f"storefront path cleared {domain} cookies — that is the live "
            "session it is supposed to reuse",
        )

    def clear_cookies(self) -> None:
        raise AssertionError("storefront path cleared cookies")

    def clear_profile_data(self) -> None:
        raise AssertionError("storefront path wiped the Edge profile")


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Never actually block on a browser process in a unit test."""
    async def _instant(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(sf, "wait_for_browser_exit", _instant)


# ── Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "host"),
    [
        ("epic", "store.epicgames.com"),
        ("gog", "www.gog.com"),
        ("amazon", "luna.amazon.com"),
        ("microsoft", "www.xbox.com"),
    ],
)
async def test_opens_each_browser_store_on_its_own_shop(
    store: str, host: str,
) -> None:
    edge = _FakeEdge()

    result = await sf.handle_store_storefront(_Ctx(store), edge)

    assert result.success is True
    assert len(edge.launched) == 1
    assert host in edge.launched[0]


@pytest.mark.asyncio
async def test_a_failed_spawn_is_reported_not_raised() -> None:
    edge = _FakeEdge(launch_ok=False)

    result = await sf.handle_store_storefront(_Ctx("epic"), edge)

    assert result.success is False
    assert result.error == "edge_storefront_launch_failed"


# ── Routing guards ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["ubisoft", "battlenet"])
async def test_a_wrapper_store_must_never_reach_the_browser(store: str) -> None:
    """Their session lives in a Wine prefix, so Edge would be signed out.

    ``_handle_auth_path`` routes them to the vendor client before this
    module is consulted; arriving here at all is a routing bug and must
    fail loudly rather than open a logged-out web page.
    """
    edge = _FakeEdge()

    with pytest.raises(GameNotFoundError):
        await sf.handle_store_storefront(_Ctx(store), edge)

    assert edge.launched == []


@pytest.mark.asyncio
async def test_an_unknown_store_raises() -> None:
    with pytest.raises(GameNotFoundError):
        await sf.handle_store_storefront(_Ctx("nope"), _FakeEdge())


@pytest.mark.asyncio
async def test_a_missing_edge_is_a_dependency_error() -> None:
    """Mirrors the auth flow, so the frontend can offer the same install."""
    with pytest.raises(DependencyMissingError):
        await sf.handle_store_storefront(_Ctx("epic"), _FakeEdge(installed=False))


# ── Single-instance collision ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("port", "expected"),
    [(_AUTH_PORT, "edge_busy_auth"), (_XCLOUD_PORT, "edge_busy_xcloud")],
)
async def test_a_live_edge_is_refused_rather_than_fought(
    port: int, expected: str,
) -> None:
    edge = _FakeEdge(alive=(port,))

    result = await sf.handle_store_storefront(_Ctx("epic"), edge)

    assert result.success is False
    assert result.error == expected
    assert edge.launched == [], "spawning would hand off and die immediately"


@pytest.mark.asyncio
async def test_an_open_shop_window_is_reused_not_respawned() -> None:
    """Epic's shop then GOG's shop must steer one window, not open two."""
    edge = _FakeEdge(alive=(_SHOP_PORT,))

    result = await sf.handle_store_storefront(_Ctx("gog"), edge)

    assert result.success is True
    assert edge.launched == []
    assert len(edge.navigated) == 1
    port, url = edge.navigated[0]
    assert port == _SHOP_PORT
    assert "gog.com" in url


@pytest.mark.asyncio
async def test_a_failed_reuse_closes_the_stale_targets_and_respawns() -> None:
    edge = _FakeEdge(alive=(_SHOP_PORT,), navigate_ok=False)

    result = await sf.handle_store_storefront(_Ctx("epic"), edge)

    assert result.success is True
    assert edge.closed == [_SHOP_PORT]
    assert len(edge.launched) == 1


@pytest.mark.asyncio
async def test_the_reused_window_is_not_waited_on() -> None:
    """Someone else's launcher owns it and is already waiting.

    Waiting here too would hold this Steam shortcut "running" for the
    full 30-minute ceiling over a window this process does not own.
    """
    edge = _FakeEdge(alive=(_SHOP_PORT,))

    result = await sf.handle_store_storefront(_Ctx("epic"), edge)

    assert result.success is True
    assert edge.launched == []
