"""Tests for handing the saved Microsoft sign-in to xbox.com in the stream window.

``stores/microsoft/xbox_com_grant`` turns the saved device-code sign-in into
the token answer xbox.com's device-code page expects;
``launcher/flows/xbox_com_signin`` gives it to the page through the
``globalThis.deviceCode`` host provider; ``run_browser_game`` runs that beside
every stream and cancels it when the window closes.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.launcher.flows import xbox_com_signin
from unifideck.launcher.flows.xbox_com_signin import device_code_url, provider_script
from unifideck.stores.microsoft import xbox_com_grant
from unifideck.stores.microsoft.tokens.endpoint import HttpReply
from unifideck.stores.microsoft.xbox_com_grant import XBOX_COM_CLIENT_ID, load_xbox_com_grant

ANSWER = {"access_token": "at", "refresh_token": "rt2", "id_token": "id", "client_info": "ci"}


@pytest.fixture()
def saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Write a 'saved sign-in' (decryption faked) and capture the refresh POST."""
    path = tmp_path / "microsoft_tokens.json"
    posts: list[dict[str, str]] = []

    def write(payload: dict[str, Any], reply: HttpReply | None = None) -> str:
        path.write_bytes(json.dumps(payload).encode())
        monkeypatch.setattr(
            xbox_com_grant.SecureTokenStore, "decrypt_payload",
            lambda _self, blob: json.loads(blob),
        )

        def post(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
            posts.append({"url": url, **params})
            return reply or HttpReply(200, dict(ANSWER))

        monkeypatch.setattr(xbox_com_grant, "post_form", post)
        return str(path)

    write.posts = posts  # type: ignore[attr-defined]
    return write


def test_no_saved_signin_names_where_it_looked(tmp_path: Path) -> None:
    grant = load_xbox_com_grant(str(tmp_path / "missing.json"))
    assert not grant.ok and str(tmp_path / "missing.json") in grant.reason


def test_a_signin_for_another_client_is_not_handed_over(saved: Any) -> None:
    path = saved({"refresh_token": "rt", "client_id": "000000004C12AE6F"})
    grant = load_xbox_com_grant(path)
    assert not grant.ok and "000000004C12AE6F" in grant.reason
    assert saved.posts == []


def test_xbox_com_signin_is_refreshed_with_xbox_com_scopes(saved: Any) -> None:
    path = saved({"refresh_token": "rt", "client_id": XBOX_COM_CLIENT_ID})
    grant = load_xbox_com_grant(path)
    assert grant.ok and grant.body == ANSWER
    (post,) = saved.posts
    assert post["client_id"] == XBOX_COM_CLIENT_ID and post["refresh_token"] == "rt"
    assert "openid" in post["scope"].split() and post["client_info"] == "1"


def test_a_refused_refresh_gives_the_reason_not_the_token(saved: Any) -> None:
    path = saved({"refresh_token": "rt", "client_id": XBOX_COM_CLIENT_ID},
                 HttpReply(400, {"error": "invalid_grant"}))
    grant = load_xbox_com_grant(path)
    assert not grant.ok and "invalid_grant" in grant.reason and "rt" not in grant.reason


def test_grant_repr_never_shows_tokens() -> None:
    assert "rt2" not in repr(xbox_com_grant.XboxComGrant(body=dict(ANSWER)))


def test_provider_answers_only_on_xbox_com_with_the_token() -> None:
    script = provider_script(ANSWER)
    assert "location.hostname !== 'www.xbox.com'" in script
    assert "globalThis.deviceCode" in script
    # The token answer is embedded as a JSON string the page can JSON.parse.
    assert json.dumps(json.dumps(ANSWER)) in script


@pytest.mark.parametrize(("page", "expected"), [
    ("https://www.xbox.com/en-IN/play/launch/deathloop/9P5Z4530318L", "https://www.xbox.com/en-IN/play/login/deviceCode"),
    ("https://www.xbox.com/play/launch/9P5Z4530318L", "https://www.xbox.com/play/login/deviceCode"),
])
def test_device_code_page_keeps_the_page_locale(page: str, expected: str) -> None:
    assert device_code_url(page) == f"{expected}?redirectUrl=%2Fplay"


async def test_no_xbox_page_means_failed_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_page(_port: int, _timeout: float) -> None:
        return None

    monkeypatch.setattr(xbox_com_signin, "xbox_page", no_page)
    assert await xbox_com_signin.ensure_xbox_com_session(9223, "https://www.xbox.com/play", "x") == "failed"


def _service(launched: list[str]) -> Any:
    async def emit(*_a: Any, **_k: Any) -> None:
        return None

    return SimpleNamespace(
        _edge_browser=SimpleNamespace(
            is_installed=True, process=None,
            launch_browser_game=lambda url, **_k: launched.append(url) or True,
            browser_game_cdp_port=lambda: 9223,
        ),
        _bus=SimpleNamespace(emit=emit), _config=None, _active_subprocess=None,
    )


@pytest.mark.parametrize(("kind", "url", "hands_over"), [
    ("stream", "https://www.xbox.com/play/launch/9P5Z4530318L", True),
    ("web", "https://html-classic.itch.zone/html/1/index.html", False),
])
async def test_streams_hand_over_the_signin_and_cancel_it_on_close(
    monkeypatch: pytest.MonkeyPatch, kind: str, url: str, hands_over: bool,
) -> None:
    from unifideck.services.launcher import browser_game

    calls: list[tuple[int, str, str]] = []
    cancelled = asyncio.Event()

    async def fake_session(port: int, launch_url: str, token_file: str) -> str:
        calls.append((port, launch_url, token_file))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "signed_in"

    async def no_stage(*_a: Any, **_k: Any) -> None:
        return None

    async def window_closes(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0)

    from unifideck.launcher.flows import xcloud_stream
    monkeypatch.setattr(xcloud_stream, "prepare_stream", fake_session)
    monkeypatch.setattr(browser_game, "emit_stage", no_stage)
    monkeypatch.setattr(browser_game, "wait_for_browser_exit", window_closes)
    monkeypatch.setattr(browser_game, "is_allowed_browser_url", lambda *_a: True)

    async def own_player_unavailable(*_a: Any) -> None:
        return None

    from unifideck.services.launcher import xcloud_player_game
    monkeypatch.setattr(xcloud_player_game, "try_own_player", own_player_unavailable)
    launched: list[str] = []
    ctx = SimpleNamespace(browser_url=url, browser_kind=kind, store="microsoft",
                          game_id="1", game_key="microsoft:1")

    result = await browser_game.run_browser_game(_service(launched), ctx)

    assert result.success and launched == [url]
    if hands_over:
        assert calls == [(9223, url, xbox_com_grant.DEFAULT_TOKEN_FILE)]
        assert cancelled.is_set()
    else:
        assert calls == []


class _FakeSession:
    """A PageSession whose evaluations come from a script."""

    def __init__(self, values: list[Any]) -> None:
        self.values, self.calls = list(values), []

    async def value(self, _expression: str) -> Any:
        return self.values.pop(0) if self.values else None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params))
        return {}


async def test_unknown_session_state_is_retried_not_read_as_signed_out() -> None:
    """The kiosk starts on a blank page: its unreadable storage must not count as signed out."""
    from unifideck.launcher.flows.xbox_com_signin import PageSession

    session = _FakeSession([None, None, True])
    assert await PageSession.known(session, "x", 5) is True  # type: ignore[arg-type]


async def test_session_state_never_known_is_none() -> None:
    from unifideck.launcher.flows.xbox_com_signin import PageSession

    session = _FakeSession([])
    assert await PageSession.known(session, "x", 0.6) is None  # type: ignore[arg-type]


def test_has_session_reports_nothing_off_xbox_com() -> None:
    from unifideck.launcher.flows.xbox_com_signin import HAS_SESSION_JS

    assert "location.hostname !== 'www.xbox.com'" in HAS_SESSION_JS
    assert "return null" in HAS_SESSION_JS and "return false" not in HAS_SESSION_JS


def test_start_gate_needs_a_live_stream_and_one_button() -> None:
    """Error popups share the component but have no live stream: never pressed."""
    from unifideck.launcher.flows.xcloud_stream import GATE_JS

    assert "getVideoTracks" in GATE_JS and "'live'" in GATE_JS
    assert "button[class*=" in GATE_JS and "b.length !== 1" in GATE_JS


async def test_start_gate_is_clicked_once_where_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.launcher.flows import xcloud_stream

    session = _FakeSession([None, {"x": 10, "y": 20}])

    async def page(_port: int, _timeout: float) -> dict[str, Any]:
        return {"webSocketDebuggerUrl": "ws://x"}

    class _Ws:
        async def __aenter__(self) -> _Ws:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _Http:
        async def __aenter__(self) -> _Http:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        def ws_connect(self, *_a: Any, **_k: Any) -> _Ws:
            return _Ws()

    monkeypatch.setattr(xcloud_stream, "xbox_page", page)
    monkeypatch.setattr(xcloud_stream.aiohttp, "ClientSession", _Http)
    monkeypatch.setattr(xcloud_stream, "PageSession", lambda _ws: session)
    monkeypatch.setattr(xcloud_stream.asyncio, "sleep", _no_sleep)

    assert await xcloud_stream.press_start_gate(9223, timeout=5) == "pressed"
    clicks = [p for m, p in session.calls if m == "Input.dispatchMouseEvent"]
    assert [c["type"] for c in clicks] == ["mousePressed", "mouseReleased"]
    assert all((c["x"], c["y"]) == (10, 20) for c in clicks)


async def _no_sleep(_s: float) -> None:
    return None
