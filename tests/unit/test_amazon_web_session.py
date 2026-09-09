"""``stores.amazon.web_session`` — turning nile's token into a web session.

nile signs in through Amazon's device-registration flow. That
authorises the device and yields working store credentials, but leaves
the shared Edge profile with only tracking cookies — so the QAM cart
opened amazon.com logged out while nile itself worked fine. Amazon's
own apps exchange a refresh token for website cookies; this module is
that exchange.

Every test here is offline. The shape of the response was captured from
a live call: ``response.tokens.cookies[".amazon.com"]`` holding
``at-main``, ``sess-at-main``, ``session-id``, ``ubid-main`` and
``x-main``.

The behaviour that matters most is the failure handling. This runs on
the way to opening a shop, so it must degrade to "open it anyway",
never to an error the user sees instead of their store.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from unifideck.stores.amazon import web_session as ws

_LIVE_SHAPE = {
    "response": {
        "tokens": {
            "cookies": {
                ".amazon.com": [
                    {"Name": "at-main", "Value": "Atza|x", "Secure": True,
                     "HttpOnly": True, "Path": "/"},
                    {"Name": "x-main", "Value": "xm", "Secure": True},
                    {"Name": "session-id", "Value": "123-456"},
                ],
            },
        },
    },
}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    """A refresh token is present unless a test says otherwise."""
    monkeypatch.setattr(ws, "_read_refresh_token", lambda: "Atnr|token")


def _serve(monkeypatch, payload: dict) -> list[dict]:
    """Stub the HTTP call; return the list the request bodies land in."""
    seen: list[dict] = []

    class _Response:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _urlopen(request, **_kw):
        seen.append(json.loads(request.data))
        return _Response()

    monkeypatch.setattr(ws.urllib.request, "urlopen", _urlopen)
    return seen


@pytest.mark.asyncio
async def test_it_returns_the_cookies_amazon_sends(monkeypatch) -> None:
    _serve(monkeypatch, _LIVE_SHAPE)

    cookies = await ws.fetch_website_cookies()

    assert sorted(c["name"] for c in cookies) == [
        "at-main", "session-id", "x-main",
    ]
    at_main = next(c for c in cookies if c["name"] == "at-main")
    assert at_main["host"] == ".amazon.com"
    assert at_main["value"] == "Atza|x"
    assert at_main["httponly"] is True


@pytest.mark.asyncio
async def test_it_asks_for_auth_cookies_against_niles_app(
    monkeypatch,
) -> None:
    """Amazon keys the exchange on the app the token was issued to.

    A different ``app_name`` gets the request rejected, which is the
    kind of failure that would look like "Amazon just logs me out".
    """
    seen = _serve(monkeypatch, _LIVE_SHAPE)

    await ws.fetch_website_cookies()

    body, = seen
    assert body["requested_token_type"] == "auth_cookies"
    assert body["source_token_type"] == "refresh_token"
    assert body["source_token"] == "Atnr|token"
    assert body["app_name"] == "AGSLauncher for Windows"
    assert body["domain"] == ".amazon.com"


@pytest.mark.asyncio
async def test_cookies_default_to_secure_and_root_path(monkeypatch) -> None:
    _serve(monkeypatch, _LIVE_SHAPE)

    cookies = await ws.fetch_website_cookies()

    session_id = next(c for c in cookies if c["name"] == "session-id")
    assert session_id["path"] == "/"
    assert session_id["secure"] is True


# ── Degrade quietly, always ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_nile_credentials_yields_nothing(monkeypatch) -> None:
    monkeypatch.setattr(ws, "_read_refresh_token", lambda: "")

    assert await ws.fetch_website_cookies() == []


@pytest.mark.asyncio
async def test_a_rejected_token_yields_nothing(monkeypatch) -> None:
    """An expired refresh token must not break the cart."""
    def _raise(*_a, **_kw):
        raise urllib.error.HTTPError(ws._TOKEN_URL, 401, "no", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(ws.urllib.request, "urlopen", _raise)

    assert await ws.fetch_website_cookies() == []


@pytest.mark.asyncio
async def test_a_network_failure_yields_nothing(monkeypatch) -> None:
    def _raise(*_a, **_kw):
        raise OSError("offline")

    monkeypatch.setattr(ws.urllib.request, "urlopen", _raise)

    assert await ws.fetch_website_cookies() == []


@pytest.mark.asyncio
async def test_an_unexpected_body_yields_nothing(monkeypatch) -> None:
    _serve(monkeypatch, {"response": {}})

    assert await ws.fetch_website_cookies() == []


@pytest.mark.asyncio
async def test_entries_missing_a_name_or_value_are_skipped(
    monkeypatch,
) -> None:
    _serve(monkeypatch, {"response": {"tokens": {"cookies": {
        ".amazon.com": [
            {"Name": "", "Value": "v"},
            {"Name": "n"},
            {"Name": "good", "Value": "v"},
        ],
    }}}})

    cookies = await ws.fetch_website_cookies()

    assert [c["name"] for c in cookies] == ["good"]
