"""One XBL user token serves every relying party, minted the right way.

- ``build_rp_chain`` reuses an XBL user token it is given, and otherwise a
  cached one until shortly before ``NotAfter``, so a sync that needs three
  relying parties (xboxlive.com, gssv, mp.microsoft.com) signs in once.
- The cache is tied to the access token: after a sign-out or a new sign-in
  the old user token is never reused.
- The RpsTicket prefix follows the client: ``d=`` first for xbox.com's
  device-code client, ``t=`` first for the login.live.com client.
- A refused XSTS is ``None``, with the XErr spelled out.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from unifideck.stores.microsoft import microsoft_auth
from unifideck.stores.microsoft.microsoft_config import (
    AUTH_FLOW_BROWSER_REDIRECT,
    AUTH_FLOW_DEVICE_CODE,
    MicrosoftConfig,
)
from unifideck.stores.microsoft.tokens import xbl_chain
from unifideck.stores.microsoft.tokens.xbl_chain import XBLChainMixin


class _Chain(XBLChainMixin):
    def __init__(self, flow: str = AUTH_FLOW_DEVICE_CODE) -> None:
        self._config = MicrosoftConfig(auth_flow=flow, xsts_url="https://xsts", xbl_auth_url="https://xbl")
        self._locale_fn = lambda: "en-US"
        self._ms_access_token = "access-1"
        self._xbl_user = None


def _xsts_ok(rp: str) -> dict[str, Any]:
    return {"Token": f"xsts-{rp}", "DisplayClaims": {"xui": [{"uhs": "uhs1", "xid": "123"}]}}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"user": [], "xsts": []}

    def user_token(access: str, locale: str, url: str, ua: str, *, prefer_d: bool = False) -> dict[str, Any]:
        seen["user"].append((access, prefer_d))
        return {"Token": f"xbl-{access}", "NotAfter": "2999-01-01T00:00:00Z",
                "DisplayClaims": {"xui": [{"uhs": "uhs1"}]}}

    def xsts(xbl_token: str, xsts_rp: str, locale: str, xsts_url: str, xbl_user_agent: str) -> dict[str, Any]:
        seen["xsts"].append((xbl_token, xsts_rp))
        return _xsts_ok(xsts_rp)

    monkeypatch.setattr(xbl_chain, "_obtain_xbl_user_token", user_token)
    monkeypatch.setattr(xbl_chain, "request_xsts_token", xsts)
    return seen


async def test_three_relying_parties_one_user_token(calls: dict[str, list[Any]]) -> None:
    chain = _Chain()
    std = await chain.build_chain()
    gssv = await chain.build_gssv_chain()
    mp = await chain.build_marketplace_chain()
    assert std and gssv and mp
    assert len(calls["user"]) == 1
    assert [rp for _, rp in calls["xsts"]] == [
        "http://xboxlive.com", "http://gssv.xboxlive.com/", "http://mp.microsoft.com/",
    ]
    assert std.xuid == "123"
    assert mp.xsts_token == "xsts-http://mp.microsoft.com/"


async def test_given_user_token_is_reused(calls: dict[str, list[Any]]) -> None:
    chain = _Chain()
    result = await chain.build_gssv_chain(xbl_token="given")
    assert result and result.xbl_token == "given"
    assert calls["user"] == []


async def test_new_access_token_mints_a_new_user_token(calls: dict[str, list[Any]]) -> None:
    chain = _Chain()
    await chain.build_chain()
    chain._ms_access_token = "access-2"
    await chain.build_chain()
    assert [a for a, _ in calls["user"]] == ["access-1", "access-2"]


async def test_signed_out_builds_nothing(calls: dict[str, list[Any]]) -> None:
    chain = _Chain()
    chain._ms_access_token = None
    assert await chain.build_chain() is None


async def test_expiring_user_token_is_replaced(calls: dict[str, list[Any]]) -> None:
    chain = _Chain()
    await chain.build_chain()
    assert chain._xbl_user is not None
    chain._xbl_user = xbl_chain._UserToken("old", "uhs1", time.time() + 10, "access-1")
    await chain.build_chain()
    assert len(calls["user"]) == 2


@pytest.mark.parametrize(("flow", "prefer_d"), [
    (AUTH_FLOW_DEVICE_CODE, True), (AUTH_FLOW_BROWSER_REDIRECT, False),
])
async def test_ticket_preference_follows_the_flow(
    calls: dict[str, list[Any]], flow: str, prefer_d: bool,
) -> None:
    await _Chain(flow).build_chain()
    assert calls["user"] == [("access-1", prefer_d)]


async def test_refused_xsts_is_none(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]) -> None:
    monkeypatch.setattr(xbl_chain, "request_xsts_token", lambda *a, **k: {"XErr": 2148916233})
    assert await _Chain().build_chain() is None


# ── the ticket order itself ──────────────────────────────────────────

@pytest.mark.parametrize(("prefer_d", "first"), [(True, ("1", "d=")), (False, ("2", "t="))])
def test_ticket_order(monkeypatch: pytest.MonkeyPatch, prefer_d: bool, first: tuple[str, str]) -> None:
    tried: list[tuple[str, str]] = []

    def attempt(contract: str, rps: str, *rest: Any) -> None:
        tried.append((contract, rps[:2]))
        return

    monkeypatch.setattr(microsoft_auth, "_try_xbl_request", attempt)
    microsoft_auth._obtain_xbl_user_token("tok", "en-US", "https://xbl", "ua", prefer_d=prefer_d)
    assert tried[0] == first


@pytest.mark.parametrize(("code", "words"), [
    (2148916233, "no Xbox profile"),
    (2148916235, "region"),
    (2148916238, "child account"),
    (1, "unknown"),
])
def test_xerr_meanings(code: int, words: str) -> None:
    assert words in microsoft_auth.describe_xerr(code)
