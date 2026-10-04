"""Tests for ``stores.itch.auth``: Edge sign-in → API key → butler profile.

The capture expression and regex are checked against the markup the
itch.io API-keys page served on 2026-09-23 (key masked to a fixed dummy).
"""
from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.types import AuthResult, Events
from unifideck.stores.itch.auth import KEY_CAPTURE, ItchAuthFlow, should_steer
from unifideck.stores.itch.butlerd import ButlerdError

DUMMY_KEY = "A" * 20 + "b" * 20  # 40 alphanumerics, the measured key shape


class _Daemon:
    available = True

    def __init__(self, *, profiles: list[dict[str, Any]] | None = None,
                 saved_ok: bool = True, login_ok: bool = True) -> None:
        self.profiles = profiles if profiles is not None else []
        self.saved_ok = saved_ok
        self.login_ok = login_ok
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        if method == "Profile.List":
            return {"profiles": self.profiles}
        if method == "Profile.UseSavedLogin":
            if not self.saved_ok:
                raise ButlerdError("itch.io API error (403)", code=12000, api_status=403)
            return {"profile": self.profiles[0]}
        if method == "Profile.LoginWithAPIKey":
            if not self.login_ok:
                raise ButlerdError("invalid key", code=12000, api_status=403)
            return {"profile": {"id": 7, "user": {"username": "someone"}}}
        if method == "Profile.Forget":
            return {"success": True}
        raise AssertionError(method)


def _flow(daemon: _Daemon) -> tuple[ItchAuthFlow, MagicMock, MagicMock]:
    bus = MagicMock()
    bus.emit = AsyncMock()
    orch = MagicMock()
    orch.run_flow = AsyncMock(return_value=AuthResult(success=True, store="itch", url="https://itch.io/login"))
    return ItchAuthFlow(bus, orch, daemon, lambda: None), bus, orch


def test_capture_reads_the_hidden_key_element_not_inner_text() -> None:
    # The key sits in a <code class="full_key"> that stays hidden until "View";
    # innerText never contains it, so the default expression cannot be used.
    assert "code.full_key" in KEY_CAPTURE.expression
    assert "innerText" not in KEY_CAPTURE.expression
    assert KEY_CAPTURE.trigger_url == "itch.io/user/settings/api-keys"


@pytest.mark.parametrize(("text", "expected"), [
    (f"{DUMMY_KEY}", DUMMY_KEY),
    (f"  {DUMMY_KEY}\n", DUMMY_KEY),        # textContent keeps markup whitespace
    ("", None),                             # no key generated yet
    ("3Tskz8O...", None),                   # the truncated display text
    (DUMMY_KEY + "X", None),                # 41 chars is not a key
])
def test_key_regex(text: str, expected: str | None) -> None:
    m = re.search(KEY_CAPTURE.regex, text)
    assert (m.group(1) if m else None) == expected


@pytest.mark.parametrize(("url", "steer"), [
    ("https://itch.io/my-feed", True),       # where a password reset dropped the user
    ("https://itch.io/", True),
    ("https://itch.io/login?return_to=x", False),
    ("https://itch.io/sudo?return_to=x", False),
    ("https://itch.io/user/settings/api-keys", False),
    ("https://itch.io/user/forgot-password", False),
    ("https://someone.itch.io/game", False),  # a creator page, not ours to redirect
    ("http://itch.io/my-feed", False),
])
def test_should_steer(url: str, steer: bool) -> None:
    assert should_steer(url) is steer


async def test_valid_saved_login_skips_the_browser() -> None:
    flow, bus, orch = _flow(_Daemon(profiles=[{"id": 7}]))
    result = await flow.start_auth()
    assert result.success
    orch.run_flow.assert_not_called()
    bus.emit.assert_awaited_with(Events.STORE_AUTH_COMPLETE, store="itch")


async def test_rejected_saved_login_opens_the_edge_flow_with_the_key_capture() -> None:
    flow, _bus, orch = _flow(_Daemon(profiles=[{"id": 7}], saved_ok=False))
    result = await flow.start_auth()
    assert result.success and result.url
    kwargs = orch.run_flow.await_args.kwargs
    assert kwargs["content"] is KEY_CAPTURE
    assert kwargs["background"] is True
    assert kwargs["write_url_file"].endswith("itch_auth_url.txt")
    flow._stop_steering()


async def test_captured_key_is_handed_to_butler() -> None:
    daemon = _Daemon()
    flow, _bus, _orch = _flow(daemon)
    result = await flow._login_with_key(DUMMY_KEY)
    assert result.success
    assert ("Profile.LoginWithAPIKey", {"apiKey": DUMMY_KEY}) in daemon.calls


async def test_a_rejected_key_is_a_failed_exchange() -> None:
    flow, _bus, _orch = _flow(_Daemon(login_ok=False))
    result = await flow._login_with_key(DUMMY_KEY)
    assert not result.success and result.error.startswith("itch_login_failed")


async def test_logout_forgets_every_profile() -> None:
    daemon = _Daemon(profiles=[{"id": 7}, {"id": 8}])
    flow, _bus, _orch = _flow(daemon)
    assert (await flow.logout()).success
    assert [p for m, p in daemon.calls if m == "Profile.Forget"] == [{"profileId": 7}, {"profileId": 8}]
