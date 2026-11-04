"""Tests for the xCloud sign-in chained onto the Xbox PC one.

Xbox PC and xCloud need two different Microsoft credentials, so Connect runs
both in one window. These pin the parts that matter: it is skipped when a
sign-in is already stored, it drives the window that is already open, it never
reports the store as failed when only this step fails, and the window is not
closed until it has finished.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.stores.microsoft.tokens.endpoint import HttpReply
from unifideck.stores.microsoft.xodus import signin_chain
from unifideck.stores.microsoft.xodus.signin_chain import report_chain_outcome, run_chained_signin

GRANT = {"device_code": "dc", "user_code": "ABCD-EFGH",
         "verification_uri": "https://www.microsoft.com/link", "expires_in": 900, "interval": 0}
TOKENS_BODY = {"access_token": "at", "refresh_token": "rt"}


class FakePage:
    """A CdpPageSession that records what the window was told to do."""

    def __init__(self, *, attached: bool = True) -> None:
        self.attached, self.navigated, self.closed = attached, [], False

    async def attach(self, _port: int, wait_s: float = 0.0) -> bool:
        return self.attached

    async def call(self, _method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    async def post(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method == "Page.navigate":
            self.navigated.append((params or {}).get("url"))

    async def close(self) -> None:
        self.closed = True


class FakeTokens:
    def __init__(self, *, has_refresh: bool = False) -> None:
        self.has_refresh_token = has_refresh
        self.accepted: list[dict[str, Any]] = []
        self.cleared = False

    async def accept_token_body(self, body: dict[str, Any]) -> None:
        self.accepted.append(body)
        self.has_refresh_token = True

    async def clear(self) -> None:
        self.cleared = True
        self.has_refresh_token = False


def _config() -> Any:
    return SimpleNamespace(
        client_id="1f907974", scope="XboxLive.signin offline_access",
        devicecode_url="https://login/devicecode", token_url="https://login/token",
        is_valid=lambda: True)


@pytest.fixture()
def posts(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Script the HTTP answers; record the requests.

    ``GRANT``'s ``interval: 0`` means "poll at once", but the parser treats a
    non-positive interval as absent and waits the 5 s default before the first
    poll. Zeroing the default makes the scripted interval the one used.
    """
    from unifideck.stores.microsoft import microsoft_device_auth

    monkeypatch.setattr(microsoft_device_auth, "_DEFAULT_INTERVAL", 0)
    sent: list[dict[str, str]] = []

    def install(*replies: HttpReply) -> list[dict[str, str]]:
        queue = list(replies)

        def post(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
            sent.append({"url": url, **params})
            return queue.pop(0) if queue else HttpReply(400, {"error": "unexpected_call"})

        monkeypatch.setattr(signin_chain, "post_form", post)
        return sent

    return install


async def _ok() -> None:
    return None


async def test_already_signed_in_is_not_asked_again(posts: Any) -> None:
    sent = posts()
    page = FakePage()
    outcome = await run_chained_signin(9222, _config(), FakeTokens(has_refresh=True), _ok,
                                       lambda: page)  # type: ignore[arg-type]
    assert outcome == "skipped"
    assert sent == [] and page.navigated == []


async def test_the_open_window_is_sent_to_the_code_page(posts: Any) -> None:
    sent = posts(HttpReply(200, dict(GRANT)), HttpReply(200, dict(TOKENS_BODY)))
    page, tokens = FakePage(), FakeTokens()
    outcome = await run_chained_signin(9222, _config(), tokens, _ok,
                                       lambda: page)  # type: ignore[arg-type]
    assert outcome == "signed_in"
    assert page.navigated == ["https://www.microsoft.com/link?otc=ABCD-EFGH"]
    assert tokens.accepted == [TOKENS_BODY] and not tokens.cleared
    assert page.closed
    # The code is asked for first, then polled against the token endpoint.
    assert [p["url"] for p in sent] == ["https://login/devicecode", "https://login/token"]


async def test_a_window_we_cannot_reach_is_reported_not_raised(posts: Any) -> None:
    posts(HttpReply(200, dict(GRANT)))
    page = FakePage(attached=False)
    outcome = await run_chained_signin(9222, _config(), FakeTokens(), _ok,
                                       lambda: page)  # type: ignore[arg-type]
    assert outcome == "window_not_opened"
    assert page.closed


async def test_an_account_xbox_refuses_does_not_keep_its_tokens(posts: Any) -> None:
    posts(HttpReply(200, dict(GRANT)), HttpReply(200, dict(TOKENS_BODY)))
    tokens = FakePage(), FakeTokens()

    async def refused() -> str:
        return "account_child"

    outcome = await run_chained_signin(9222, _config(), tokens[1], refused,
                                       lambda: tokens[0])  # type: ignore[arg-type]
    assert outcome == "account_child"
    assert tokens[1].cleared is True


async def test_a_denied_code_is_a_plain_reason(posts: Any) -> None:
    posts(HttpReply(200, dict(GRANT)), HttpReply(400, {"error": "authorization_declined"}))
    tokens = FakeTokens()
    outcome = await run_chained_signin(9222, _config(), tokens, _ok,
                                       lambda: FakePage())  # type: ignore[arg-type]
    assert outcome != "signed_in" and tokens.accepted == []


@pytest.mark.parametrize("outcome", ["signed_in", "skipped", "window_not_opened", "boom"])
async def test_reporting_never_raises_whatever_happened(outcome: str) -> None:
    async def chain(_port: int) -> str:
        if outcome == "boom":
            raise RuntimeError(outcome)
        return outcome

    await report_chain_outcome(chain, 9222)  # must not raise


async def test_reporting_lets_a_cancel_through() -> None:
    async def chain(_port: int) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await report_chain_outcome(chain, 9222)


async def test_the_window_closes_only_after_the_chain_has_run() -> None:
    """The Xbox PC sign-in must not close its window mid-chain."""
    from unifideck.stores.microsoft.xodus.auth import XboxPcSignIn

    order: list[str] = []

    async def chain(_port: int) -> str:
        order.append("chain")
        return "signed_in"

    signin = XboxPcSignIn(
        cli=SimpleNamespace(), edge=lambda: None, tokens=SimpleNamespace(),
        ms_auth_busy=lambda: False, on_change=lambda: None, chain=chain,
    )

    async def drive(_proc: Any, _port: int) -> None:
        return None

    async def cleanup(_proc: Any) -> None:
        order.append("cleanup")

    signin._drive = drive  # type: ignore[assignment]
    signin._cleanup = cleanup  # type: ignore[assignment]
    signin.verify = lambda: _ok()  # type: ignore[assignment]

    await signin._session(SimpleNamespace(), 9222)  # type: ignore[arg-type]

    assert order == ["chain", "cleanup"]
