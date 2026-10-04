"""What a Microsoft token answer means, and what refresh does about it.

Regression: the token POST raised on every HTTP error and the caller turned
every exception into ``False``, so "no network" and "Microsoft revoked the
refresh token" were the same answer and both signed the user out. An
offline Deck lost its Microsoft sign-in on the next refresh.

Pinned here:

- only an OAuth-formatted error makes a session ``DEAD``; no answer, 5xx,
  429 and HTML error pages are ``TRANSIENT``;
- a ``TRANSIENT`` refresh keeps the tokens and stays quiet for a while
  instead of retrying on every status check;
- refreshes run one at a time and the rotated refresh token is saved.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from unifideck.stores.microsoft.tokens import endpoint, oauth
from unifideck.stores.microsoft.tokens.endpoint import (
    HttpReply,
    TokenState,
    classify_token_reply,
)
from unifideck.stores.microsoft.tokens.oauth import OAuthMixin

# ── classification ────────────────────────────────────────────────────


@pytest.mark.parametrize(("reply", "state"), [
    (HttpReply(200, {"access_token": "a", "refresh_token": "r"}), TokenState.FRESH),
    (HttpReply(None, detail="URLError: [Errno -3] name resolution"), TokenState.TRANSIENT),
    (HttpReply(200, {}), TokenState.TRANSIENT),
    (HttpReply(503, {}), TokenState.TRANSIENT),
    (HttpReply(500, {"error": "server_error"}), TokenState.TRANSIENT),
    (HttpReply(429, {"error": "too_many_requests"}), TokenState.TRANSIENT),
    (HttpReply(403, {}), TokenState.TRANSIENT),  # a proxy's HTML page
    (HttpReply(400, {"error": "temporarily_unavailable"}), TokenState.TRANSIENT),
    (HttpReply(400, {"error": "invalid_grant"}), TokenState.DEAD),
    (HttpReply(400, {"error": "interaction_required"}), TokenState.DEAD),
    (HttpReply(401, {"error": "invalid_client"}), TokenState.DEAD),
    (HttpReply(400, {"error": "unauthorized_client"}), TokenState.DEAD),
    (HttpReply(400, {"error": "invalid_request"}), TokenState.DEAD),
])
def test_classification(reply: HttpReply, state: TokenState) -> None:
    assert classify_token_reply(reply)[0] is state


def test_client_rejection_is_named() -> None:
    _, reason = classify_token_reply(HttpReply(400, {"error": "unauthorized_client"}))
    assert reason.startswith("client_rejected")


def test_post_form_never_raises_offline() -> None:
    reply = endpoint.post_form("http://127.0.0.1:9/token", {"a": "b"}, timeout=2)
    assert reply.status is None
    assert classify_token_reply(reply)[0] is TokenState.TRANSIENT


# ── refresh behaviour ─────────────────────────────────────────────────


class _Config:
    token_url = "https://login.example/token"
    client_id = "client"
    redirect_uri = "https://login.example/done"
    scope = "scope"
    token_refresh_threshold_seconds = 2400


class _Tokens(OAuthMixin):
    def __init__(self) -> None:
        self._config = _Config()  # type: ignore[assignment]
        self._ms_access_token = "old-access"
        self._ms_refresh_token = "old-refresh"
        self._token_saved_at = 0.0  # stale
        self._refresh_lock = asyncio.Lock()
        self._transient_until = 0.0
        self._refresh_error = ""
        self.saves = 0

    async def save(self) -> bool:
        self.saves += 1
        return True


def _endpoint(monkeypatch: pytest.MonkeyPatch, *replies: HttpReply) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    queue = list(replies)

    def fake(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
        calls.append(params)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(oauth, "post_form", fake)
    return calls


async def test_fresh_token_skips_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = _Tokens()
    tokens._token_saved_at = time.time()
    calls = _endpoint(monkeypatch, HttpReply(200, {"access_token": "x"}))
    assert await tokens.refresh_if_stale() is TokenState.FRESH
    assert calls == []


async def test_refresh_saves_the_rotated_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = _Tokens()
    _endpoint(monkeypatch, HttpReply(200, {"access_token": "new-a", "refresh_token": "new-r"}))
    assert await tokens.refresh_if_stale() is TokenState.FRESH
    assert tokens._ms_access_token == "new-a"
    assert tokens._ms_refresh_token == "new-r"
    assert tokens.saves == 1


async def test_offline_refresh_keeps_the_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = _Tokens()
    _endpoint(monkeypatch, HttpReply(None, detail="offline"))
    assert await tokens.refresh_if_stale() is TokenState.TRANSIENT
    assert tokens._ms_refresh_token == "old-refresh"
    assert tokens.saves == 0
    assert tokens._refresh_error.startswith("no answer")


async def test_offline_refresh_goes_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = _Tokens()
    calls = _endpoint(monkeypatch, HttpReply(None, detail="offline"))
    await tokens.refresh_if_stale()
    assert await tokens.refresh_if_stale() is TokenState.TRANSIENT
    assert len(calls) == 1  # the second answer came from memory


async def test_revoked_token_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = _Tokens()
    _endpoint(monkeypatch, HttpReply(400, {"error": "invalid_grant"}))
    assert await tokens.refresh_if_stale() is TokenState.DEAD
    assert tokens._refresh_error == "invalid_grant"


async def test_no_refresh_token_is_dead() -> None:
    tokens = _Tokens()
    tokens._ms_refresh_token = None
    assert await tokens.refresh_if_stale() is TokenState.DEAD


async def test_concurrent_refreshes_hit_the_endpoint_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Microsoft rotates the refresh token; a second racing refresh would
    present the token the first one just replaced."""
    tokens = _Tokens()
    calls: list[Any] = []

    def slow(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
        calls.append(params)
        time.sleep(0.05)
        return HttpReply(200, {"access_token": "new-a", "refresh_token": "new-r"})

    monkeypatch.setattr(oauth, "post_form", slow)
    states = await asyncio.gather(*(tokens.refresh_if_stale() for _ in range(3)))
    assert states == [TokenState.FRESH] * 3
    assert len(calls) == 1
