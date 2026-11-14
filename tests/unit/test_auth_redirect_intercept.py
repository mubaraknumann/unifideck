"""Tests for ``auth.redirect_intercept``: reading a redirect off a paused 302.

The event shapes are those Chromium 156 sent during EA's sign-in on 2026-11-15
(``Fetch.requestPaused`` at the Response stage).
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from unifideck.auth import redirect_intercept
from unifideck.auth.browser_types import RedirectIntercept
from unifideck.auth.edge_browser.cdp_session import CLOSED_EVENT

_INTERCEPT = RedirectIntercept(
    page_host="ea.com", url_pattern="https://accounts.ea.com/connect/auth*", location_prefix="qrc:",
)


def _paused(request_id: str, status: int, location: str = "") -> dict[str, Any]:
    headers = [{"name": "Content-Type", "value": "text/html"}]
    if location:
        headers.append({"name": "Location", "value": location})
    return {"method": "Fetch.requestPaused", "params": {
        "requestId": request_id, "responseStatusCode": status, "responseHeaders": headers,
        "request": {"url": "https://accounts.ea.com/connect/auth?client_id=JUNO_PC_CLIENT"},
    }}


class _Session:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in events:
            self.events.put_nowait(event)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        return {}

    async def post(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.posts.append((method, params or {}))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    made: list[_Session] = []

    async def attach(port: int, intercept: RedirectIntercept) -> _Session:
        return made[0]

    monkeypatch.setattr(redirect_intercept, "_attach", attach)
    return made


async def test_the_qrc_redirect_is_captured_and_answered_locally(session: list[_Session]) -> None:
    location = "qrc:/html/login_successful.html?code=QUOxOjMuMDo"
    session.append(_Session([
        _paused("1", 302, "/p/juno/login?execution=e1s2"),
        _paused("2", 200),
        _paused("3", 302, location),
    ]))
    assert await redirect_intercept.capture_redirect(9222, _INTERCEPT) == location

    s = session[0]
    pattern = s.calls[0][1]["patterns"][0]
    assert pattern == {"urlPattern": _INTERCEPT.url_pattern, "resourceType": "Document", "requestStage": "Response"}
    assert [m for m, _ in s.posts] == ["Fetch.continueRequest", "Fetch.continueRequest", "Fetch.fulfillRequest"]
    fulfilled = s.posts[-1][1]
    assert fulfilled["requestId"] == "3" and fulfilled["responseCode"] == 200
    assert b"Signed in" in base64.b64decode(fulfilled["body"])
    assert s.closed


async def test_a_closed_window_ends_without_a_capture(session: list[_Session]) -> None:
    session.append(_Session([_paused("1", 200), {"method": CLOSED_EVENT}]))
    assert await redirect_intercept.capture_redirect(9222, _INTERCEPT) is None
    assert session[0].closed
