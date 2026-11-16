"""The spoofer keeps its CDP session for the whole Luna session.

Chrome drops a CDP session's ``Emulation.setUserAgentOverride`` and its
pre-document scripts as soon as that session detaches. On a Steam Deck the
spoofer used to close its session right after the initial auto-attach burst,
so every later read (the launch verification, and Luna itself) saw
``Linux``. These tests pin the fix: :meth:`LunaPlatformSpoofer.attach` leaves
the session open, a background task keeps applying the override to targets
that open later, and :meth:`LunaPlatformSpoofer.close` (or the browser going
away) releases it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import unifideck.stores.luna.luna_platform_spoofer as spoofer_pkg
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_platform_spoofer import LunaPlatformSpoofer

_CONFIG = LunaConfig(
    luna_host="luna.amazon.de",
    allowed_stream_hosts=["luna.amazon.de"],
    signin_path="/signin",
    player_path="/player",
    profile_dir="/home/deck/.local/share/unifideck/luna-profile",
    browser_game_cdp_flavour="browser_game",
    spoof_ua_template=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
    ),
    spoof_uach_platform="Windows",
    spoof_navigator_platform="Win32",
    spoof_webgl_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX, Direct3D11)",
)


class _Cdp:
    def get_browser_version(self) -> str:
        return "Chrome/156.0.8078.79"

    def get_browser_ws_url(self) -> str:
        return "ws://127.0.0.1:9333/devtools/browser/x"


class _Session:
    """Browser session: one target in the burst, one opened later."""

    def __init__(self, ws_url: str) -> None:
        self.calls: list[tuple[str | None, str]] = []
        self.closed = False
        self.idle_timeouts: list[float | None] = []
        self.late_target = asyncio.Event()
        self.browser_gone = asyncio.Event()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((session_id, method))
        return {}

    async def attached_targets(
        self, idle_timeout: float | None = 2.0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.idle_timeouts.append(idle_timeout)
        if idle_timeout is not None:
            yield "burst", {"type": "page"}
            return
        await self.late_target.wait()
        yield "late", {"type": "page"}
        await self.browser_gone.wait()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Session]:
    """Swap in :class:`_Session`; the dict holds the instance attach() opens."""
    holder: dict[str, _Session] = {}

    def _factory(ws_url: str) -> _Session:
        holder["s"] = _Session(ws_url)
        return holder["s"]

    monkeypatch.setattr(spoofer_pkg, "_AiohttpBrowserCdpSession", _factory)
    return holder


def _resumed(s: _Session, sid: str) -> bool:
    return (sid, "Runtime.runIfWaitingForDebugger") in s.calls


async def test_attach_keeps_session_open_and_covers_late_targets(
    session: dict[str, _Session],
) -> None:
    spoofer = LunaPlatformSpoofer(_CONFIG)
    await spoofer.attach(_Cdp())
    s: _Session = session["s"]

    assert _resumed(s, "burst")
    assert s.closed is False, "closing the session drops Chrome's override"

    s.late_target.set()
    for _ in range(20):
        if _resumed(s, "late"):
            break
        await asyncio.sleep(0)
    assert ("late", "Emulation.setUserAgentOverride") in s.calls
    assert _resumed(s, "late")
    assert None in s.idle_timeouts

    await spoofer.close()
    assert s.closed is True


async def test_keep_alive_releases_session_when_browser_exits(
    session: dict[str, _Session],
) -> None:
    spoofer = LunaPlatformSpoofer(_CONFIG)
    await spoofer.attach(_Cdp())
    s: _Session = session["s"]

    s.late_target.set()
    s.browser_gone.set()
    for _ in range(20):
        if s.closed:
            break
        await asyncio.sleep(0)
    assert s.closed is True
    await spoofer.close()  # idempotent once the task ended


async def test_close_before_attach_is_a_no_op() -> None:
    await LunaPlatformSpoofer(_CONFIG).close()
