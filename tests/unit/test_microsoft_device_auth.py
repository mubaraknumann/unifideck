"""The Microsoft device-code sign-in (xbox.com's client).

Pinned here, with a fake token endpoint and a fake clock:

- ``start_auth`` returns the user code for the frontend, writes the
  pre-filled ``microsoft.com/link?otc=`` page for the auth window, and never
  lets ``device_code`` out;
- the poll waits through ``authorization_pending`` and transient errors,
  slows down on ``slow_down``, and ends in exactly one terminal event;
- success saves the tokens, closes the window, removes the URL file, and
  emits ``STORE_AUTH_COMPLETE``; an account Xbox refuses (no profile, child,
  region) is a named failure, not a silent sign-in;
- ``cancel`` is silent, and a second start replaces the first.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from unifideck.core.types import Events
from unifideck.stores.microsoft import microsoft_device_auth as mda
from unifideck.stores.microsoft.microsoft_config import AUTH_FLOW_DEVICE_CODE, MicrosoftConfig
from unifideck.stores.microsoft.microsoft_device_auth import (
    MicrosoftDeviceAuth,
    parse_grant,
    poll_verdict,
    request_failure,
)
from unifideck.stores.microsoft.tokens.endpoint import HttpReply

GRANT = HttpReply(200, {
    "device_code": "DEVICE-SECRET", "user_code": "9W2495MG",
    "verification_uri": "https://www.microsoft.com/link", "expires_in": 900, "interval": 5,
})
PENDING = HttpReply(400, {"error": "authorization_pending"})
TOKENS = HttpReply(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3599})


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **payload: Any) -> None:
        self.emitted.append((event, payload))

    def events(self) -> list[Any]:
        """The store sign-in events (the audit decorator adds SECURITY_* ones)."""
        return [e for e, _ in self.emitted if str(e.value).startswith("store_")]

    def failure(self) -> str | None:
        return next((p["error"] for e, p in self.emitted if e == Events.STORE_AUTH_FAILED), None)


class _Tokens:
    def __init__(self, *, chain: bool = True, xerr: int | None = None) -> None:
        self.accepted: list[dict[str, Any]] = []
        self.cleared = False
        self._chain = chain
        self.last_xerr = xerr

    async def accept_token_body(self, body: dict[str, Any]) -> None:
        self.accepted.append(body)

    async def build_chain(self) -> object | None:
        return object() if self._chain else None

    async def clear(self) -> None:
        self.cleared = True


class _Edge:
    def __init__(self) -> None:
        self.prepared = 0
        self.closed = 0

    async def prepare_auth_launch(self) -> None:
        self.prepared += 1

    async def close_auth_browser(self) -> bool:
        self.closed += 1
        return True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("HOME", str(tmp_path))
    clock = _Clock()
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        await real_sleep(0)

    monkeypatch.setattr(mda, "_monotonic", lambda: clock.now)
    monkeypatch.setattr(mda.asyncio, "sleep", fake_sleep)
    replies: list[HttpReply] = []
    posted: list[tuple[str, dict[str, str]]] = []

    def fake_post(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
        posted.append((url, params))
        return replies.pop(0) if len(replies) > 1 else replies[0]

    monkeypatch.setattr(mda, "post_form", fake_post)
    return {
        "clock": clock, "replies": replies, "posted": posted,
        "url_file": tmp_path / ".local/share/unifideck/ms_auth_url.txt",
    }


def _auth(tokens: _Tokens | None = None) -> tuple[MicrosoftDeviceAuth, _Bus, _Tokens, _Edge]:
    config = MicrosoftConfig(
        auth_flow=AUTH_FLOW_DEVICE_CODE, client_id="client", scope="XboxLive.signin offline_access",
        token_url="https://login.example/token", devicecode_url="https://login.example/devicecode",
        xbl_auth_url="https://xbl", xsts_url="https://xsts",
        xcloud_catalog_url="https://catalog", xcloud_launch_url="https://launch",
    )
    bus, tok, edge = _Bus(), tokens or _Tokens(), _Edge()
    return MicrosoftDeviceAuth(bus, tok, config, lambda: edge), bus, tok, edge  # type: ignore[arg-type,return-value]


async def _settle(auth: MicrosoftDeviceAuth) -> None:
    assert auth._task is not None
    await auth._task


# ── start ────────────────────────────────────────────────────────────

async def test_start_hands_out_the_code_but_never_the_device_code(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, PENDING]
    auth, bus, _, edge = _auth()
    result = await auth.start_auth()
    assert result.success and result.metadata["pending"] is True
    assert result.metadata["flow"] == "device_code"
    assert result.metadata["user_code"] == "9W2495MG"
    assert "DEVICE-SECRET" not in repr(result)
    assert env["url_file"].read_text() == "https://www.microsoft.com/link?otc=9W2495MG"
    assert bus.events() == [Events.STORE_AUTH_STARTED]
    assert edge.prepared == 1
    await auth.cancel()


@pytest.mark.parametrize(("reply", "reason"), [
    (HttpReply(None, detail="offline"), "network_unreachable"),
    (HttpReply(400, {"error": "unauthorized_client"}), "client_rejected"),
    (HttpReply(500, {}), "device_code_request_failed"),
])
async def test_start_failures_are_named(env: dict[str, Any], reply: HttpReply, reason: str) -> None:
    env["replies"][:] = [reply]
    auth, bus, _, _ = _auth()
    result = await auth.start_auth()
    assert result.success is False and result.error == reason
    assert bus.failure() == reason
    assert not env["url_file"].exists()
    assert auth._task is None


# ── the poll ─────────────────────────────────────────────────────────

async def test_approval_signs_in_and_closes_the_window(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, PENDING, HttpReply(None), PENDING, TOKENS]
    auth, bus, tokens, edge = _auth()
    await auth.start_auth()
    await _settle(auth)
    assert tokens.accepted == [TOKENS.body]
    assert bus.events() == [Events.STORE_AUTH_STARTED, Events.STORE_AUTH_COMPLETE]
    assert edge.closed == 1
    assert not env["url_file"].exists()
    poll = env["posted"][1][1]
    assert poll["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert poll["device_code"] == "DEVICE-SECRET"


async def test_slow_down_slows_the_poll(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, HttpReply(400, {"error": "slow_down"}), TOKENS]
    auth, _, _, _ = _auth()
    await auth.start_auth()
    await _settle(auth)
    assert env["clock"].sleeps[:2] == [5, 10]


@pytest.mark.parametrize(("reply", "reason"), [
    (HttpReply(400, {"error": "expired_token"}), "device_code_expired"),
    (HttpReply(400, {"error": "authorization_declined"}), "access_denied"),
    (HttpReply(400, {"error": "bad_verification_code"}), "device_code_failed"),
])
async def test_terminal_answers_fail_once(env: dict[str, Any], reply: HttpReply, reason: str) -> None:
    env["replies"][:] = [GRANT, PENDING, reply]
    auth, bus, tokens, edge = _auth()
    await auth.start_auth()
    await _settle(auth)
    assert bus.failure() == reason
    assert Events.STORE_AUTH_COMPLETE not in bus.events()
    assert tokens.accepted == []
    assert edge.closed == 1
    assert not env["url_file"].exists()


async def test_the_code_expires_on_our_clock_too(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, PENDING]
    auth, bus, _, _ = _auth()
    await auth.start_auth()
    await _settle(auth)
    assert bus.failure() == "device_code_expired"
    assert env["clock"].now >= 900


@pytest.mark.parametrize(("xerr", "reason"), [
    (2148916233, "no_xbox_profile"), (2148916238, "child_account"), (2148916235, "region_unavailable"),
])
async def test_an_account_xbox_refuses_is_named(env: dict[str, Any], xerr: int, reason: str) -> None:
    env["replies"][:] = [GRANT, TOKENS]
    auth, bus, tokens, _ = _auth(_Tokens(chain=False, xerr=xerr))
    await auth.start_auth()
    await _settle(auth)
    assert bus.failure() == reason
    assert tokens.cleared is True


async def test_a_network_hiccup_after_approval_still_signs_in(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, TOKENS]
    auth, bus, tokens, _ = _auth(_Tokens(chain=False, xerr=None))
    await auth.start_auth()
    await _settle(auth)
    assert Events.STORE_AUTH_COMPLETE in bus.events()
    assert tokens.cleared is False


# ── cancel ───────────────────────────────────────────────────────────

async def test_cancel_is_silent_and_cleans_up(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, PENDING]
    auth, bus, _, edge = _auth()
    await auth.start_auth()
    await asyncio.sleep(0)
    await auth.cancel()
    assert bus.events() == [Events.STORE_AUTH_STARTED]
    assert edge.closed == 1
    assert not env["url_file"].exists()


async def test_a_second_start_replaces_the_first(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, GRANT, PENDING]
    auth, bus, _, _ = _auth()
    await auth.start_auth()
    first = auth._task
    await auth.start_auth()
    assert first is not None and first.cancelled()
    assert auth._task is not first
    assert bus.failure() is None
    await auth.cancel()


async def test_logout_cancels_clears_and_says_so(env: dict[str, Any]) -> None:
    env["replies"][:] = [GRANT, PENDING]
    auth, bus, tokens, _ = _auth()
    await auth.start_auth()
    result = await auth.logout()
    assert result.success and tokens.cleared
    assert bus.events()[-1] == Events.STORE_LOGOUT


async def test_edge_injected_after_construction_is_used(env: dict[str, Any]) -> None:
    """The store's Edge arrives after __init__ (store_injector): read it late."""
    env["replies"][:] = [GRANT, TOKENS]
    holder: dict[str, Any] = {"edge": None}
    config = MicrosoftConfig(
        auth_flow=AUTH_FLOW_DEVICE_CODE, client_id="c", scope="s",
        token_url="https://t", devicecode_url="https://d", xbl_auth_url="https://x",
        xsts_url="https://y", xcloud_catalog_url="https://z", xcloud_launch_url="https://w",
    )
    auth = MicrosoftDeviceAuth(_Bus(), _Tokens(), config, lambda: holder["edge"])  # type: ignore[arg-type]
    holder["edge"] = edge = _Edge()  # injected later
    await auth.start_auth()
    await _settle(auth)
    assert edge.prepared == 1 and edge.closed == 1


# ── the parsers ──────────────────────────────────────────────────────

def test_grant_defaults() -> None:
    grant = parse_grant(HttpReply(200, {"device_code": "d", "user_code": "U"}))
    assert grant is not None
    assert (grant.verification_uri, grant.expires_in, grant.interval) == (
        "https://www.microsoft.com/link", 900, 5,
    )


def test_no_grant_without_codes() -> None:
    assert parse_grant(HttpReply(200, {"user_code": "U"})) is None
    assert request_failure(HttpReply(400, {"error": "invalid_scope"})) == "device_code_request_failed"


@pytest.mark.parametrize(("reply", "verdict"), [
    (TOKENS, "success"),
    (PENDING, "pending"),
    (HttpReply(400, {"error": "slow_down"}), "slow_down"),
    (HttpReply(None), "transient"),
    (HttpReply(503, {}), "transient"),
    (HttpReply(429, {}), "transient"),
    (HttpReply(403, {}), "transient"),
    (HttpReply(200, {}), "transient"),
    (HttpReply(400, {"error": "invalid_client"}), "client_rejected"),
])
def test_poll_verdicts(reply: HttpReply, verdict: str) -> None:
    assert poll_verdict(reply) == verdict
