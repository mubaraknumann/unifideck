"""Tests for Unifideck's own xCloud player (launcher/xcloud_player).

They pin what the 2026-11-03 POC measured and what a user depends on: a stream
that cannot use the own player falls back to xbox.com instead of failing, the
cloud session is always ended, tokens stay out of the page, failures reach the
page as words, and the window runs in the plugin's own browser.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from unifideck.launcher.xcloud_player import api as api_mod
from unifideck.launcher.xcloud_player import browser, credentials
from unifideck.launcher.xcloud_player.api import XcloudApi, _title_ids, sdp_body
from unifideck.launcher.xcloud_player.server import PlayerServer
from unifideck.services.launcher import xcloud_player_game
from unifideck.stores.microsoft.tokens.endpoint import HttpReply


class FakeApi:
    """An XcloudApi that records calls and answers from a script."""

    def __init__(self, *, session: str = "v5/sessions/cloud/S1", state: str = "Provisioned") -> None:
        self.session, self.state, self.calls = session, state, []

    def start(self, title_id: str) -> str:
        self.calls.append(("start", title_id))
        return self.session

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path))
        return 200, {"state": self.state, "errorDetails": {"code": "X"}}

    def exchange(self, session: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((kind, session))
        return {"exchangeResponse": json.dumps({"sdp": "v=0"})}


# ── api ──────────────────────────────────────────────────────────────

def test_sdp_offer_speaks_the_player_channels() -> None:
    body = sdp_body("v=0")
    assert body["messageType"] == "offer" and body["sdp"] == "v=0"
    assert body["configuration"]["input"] == {"minVersion": 1, "maxVersion": 9}
    assert body["configuration"]["reliableinput"] == {"minVersion": 9, "maxVersion": 9}


def test_title_ids_are_read_by_product_id() -> None:
    page = {"results": [{"titleId": "DEATHLOOP", "details": {"productId": "9p5z4530318l"}},
                        {"titleId": "", "details": {"productId": "9X"}}, {"details": {}}]}
    assert _title_ids(page) == {"9P5Z4530318L": "DEATHLOOP"}


def test_a_cached_title_needs_no_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "ids.json"
    cache.write_text(json.dumps({"9P5Z4530318L": "DEATHLOOP"}))
    monkeypatch.setattr(api_mod, "title_cache", lambda: cache)
    api = XcloudApi("https://x", "gs")
    monkeypatch.setattr(api, "call", lambda *_a, **_k: pytest.fail("no request expected"))
    assert api.title_id("9p5z4530318l") == "DEATHLOOP"


def test_an_unlisted_title_is_empty_and_cached_for_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_mod, "title_cache", lambda: tmp_path / "ids.json")
    api = XcloudApi("https://x", "gs")
    monkeypatch.setattr(api, "call", lambda *_a, **_k: (200, {
        "results": [{"titleId": "OTHER", "details": {"productId": "9OTHER"}}]}))
    assert api.title_id("9MISSING") == ""
    assert json.loads((tmp_path / "ids.json").read_text()) == {"9OTHER": "OTHER"}


# ── credentials ──────────────────────────────────────────────────────

def test_no_saved_signin_gives_a_reason(tmp_path: Path) -> None:
    login = credentials.stream_login(str(tmp_path / "missing.json"))
    assert not login.ok and str(tmp_path / "missing.json") in login.reason


def test_a_refused_refresh_gives_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "saved_refresh_token", lambda _p: ("rt", ""))
    monkeypatch.setattr(credentials, "post_form",
                        lambda *_a, **_k: HttpReply(400, {"error": "invalid_grant"}))
    login = credentials.stream_login("x")
    assert not login.ok and "invalid_grant" in login.reason


def _xsts_replies(
    monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any] | None],
) -> list[dict[str, Any] | None]:
    monkeypatch.setattr(credentials, "XSTS_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(credentials, "_obtain_xbl_user_token", lambda *_a, **_k: {"Token": "U"})
    monkeypatch.setattr(credentials, "request_xsts_token", lambda **_k: replies.pop(0))
    return replies


def test_a_lost_xsts_reply_is_asked_for_again(monkeypatch: pytest.MonkeyPatch) -> None:
    left = _xsts_replies(monkeypatch, [None, {"Token": "X"}])
    monkeypatch.setattr(credentials, "_extract_user_hash", lambda _u: "H")
    assert credentials._gssv_xsts("a") == ("H", "X")
    assert left == []


def test_no_xsts_reply_is_not_called_a_refused_account(monkeypatch: pytest.MonkeyPatch) -> None:
    left = _xsts_replies(monkeypatch, [None, None])
    reason = credentials._gssv_xsts("a")
    assert isinstance(reason, str) and "refused" not in reason and "network" in reason
    assert left == []


def test_a_real_xsts_refusal_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    left = _xsts_replies(monkeypatch, [{"XErr": 2148916233}, {"Token": "never"}])
    reason = credentials._gssv_xsts("a")
    assert isinstance(reason, str) and "refused" in reason and "2148916233" in reason
    assert len(left) == 1


def test_stream_login_never_shows_tokens() -> None:
    text = repr(credentials.StreamLogin(gs_token="GS", base_uri="https://b", refresh_token="RT"))
    assert "GS" not in text and "RT" not in text


def test_transfer_ticket_asks_for_the_console_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, str]] = []

    def post(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
        sent.append({"url": url, **params})
        return HttpReply(200, {"access_token": "T"})

    monkeypatch.setattr(credentials, "post_form", post)
    assert credentials.transfer_ticket("rt") == "T"
    assert sent[0]["scope"] == credentials.TRANSFER_SCOPE
    assert sent[0]["url"] == credentials.LIVE_TOKEN_URL


# ── server ───────────────────────────────────────────────────────────

async def _client(server: PlayerServer) -> TestClient:
    await server.start()
    assert server._runner is not None
    client = TestClient(TestServer(server._runner.app))
    await client.start_server()
    return client


async def test_the_page_is_served_and_the_session_ends_on_stop() -> None:
    api = FakeApi()
    server = PlayerServer(api, "DEATHLOOP", lambda: "T")  # type: ignore[arg-type]
    client = await _client(server)
    try:
        assert server.url.startswith("http://127.0.0.1:")
        page = await client.get("/")
        assert page.status == 200 and "bundle.js" in await page.text()
        assert (await client.post("/api/start", json={})).status == 200
    finally:
        await client.close()
        await server.stop()
    assert ("DELETE", "/v5/sessions/cloud/S1") in api.calls


async def test_a_refused_start_reaches_the_page_as_words() -> None:
    server = PlayerServer(FakeApi(session=""), "X", lambda: "T")  # type: ignore[arg-type]
    client = await _client(server)
    try:
        reply = await client.post("/api/start", json={})
        assert reply.status == 502 and "Game Pass" in (await reply.json())["message"]
    finally:
        await client.close()
        await server.stop()


async def test_no_connect_ticket_tells_the_user_to_sign_in_again() -> None:
    server = PlayerServer(FakeApi(), "X", lambda: "")  # type: ignore[arg-type]
    client = await _client(server)
    try:
        await client.post("/api/start", json={})
        reply = await client.post("/api/connect", json={})
        assert reply.status == 502 and "Sign in" in (await reply.json())["message"]
    finally:
        await client.close()
        await server.stop()


async def test_tokens_never_reach_the_page() -> None:
    server = PlayerServer(FakeApi(state="Failed"), "X", lambda: "SECRET")  # type: ignore[arg-type]
    client = await _client(server)
    try:
        await client.post("/api/start", json={})
        for path in ("/api/state",):
            assert "SECRET" not in await (await client.get(path)).text()
        assert "SECRET" not in await (await client.post("/api/connect", json={})).text()
    finally:
        await client.close()
        await server.stop()


# ── browser ──────────────────────────────────────────────────────────

def test_the_plugins_browser_is_used() -> None:
    assert browser.browser_cmd(["/b/chrome"]) == (["/b/chrome"], "chromium")


def test_no_browser_means_no_own_player() -> None:
    assert browser.browser_cmd(None) is None


# ── orchestration ────────────────────────────────────────────────────

def _svc(config: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        _config=SimpleNamespace(get=lambda k, d=None: (config or {}).get(k, d)),
        _active_subprocess=None, _edge_browser=SimpleNamespace(find_cmd=lambda: None))


_CTX = SimpleNamespace(store="microsoft", game_key="microsoft:9P5Z4530318L")
_URL = "https://www.xbox.com/play/launch/9P5Z4530318L"


async def test_turned_off_in_config_means_xbox_com() -> None:
    assert await xcloud_player_game.try_own_player(
        _svc({"launcher.xcloud_player": "xbox_com"}), _CTX, _URL) is None


async def test_no_saved_signin_falls_back_to_xbox_com(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "stream_login",
                        lambda _f: credentials.StreamLogin(reason="no saved Microsoft sign-in"))
    assert await xcloud_player_game.try_own_player(_svc(), _CTX, _URL) is None


async def test_a_stream_plays_and_always_ends_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[str] = []
    monkeypatch.setattr(credentials, "stream_login", lambda _f: credentials.StreamLogin(
        gs_token="gs", base_uri="https://b", refresh_token="rt"))
    monkeypatch.setattr(XcloudApi, "title_id", lambda _self, pid: "DEATHLOOP")
    monkeypatch.setattr(browser, "browser_cmd", lambda _edge: (["chrome"], "chrome"))

    class Proc:
        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(browser, "launch", lambda _cmd, _url: Proc())
    monkeypatch.setattr(xcloud_player_game, "_end", lambda _proc: ended.append("window"))

    async def stop(self: PlayerServer) -> None:
        ended.append("stopped")

    monkeypatch.setattr(PlayerServer, "stop", stop)
    result = await xcloud_player_game.try_own_player(_svc(), _CTX, _URL)
    assert result is not None and result.success
    assert ended == ["window", "stopped"]


async def test_a_cancelled_stream_still_ends_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Steam's Stop / Exit game cancels the launch: the cloud session must not leak."""
    ended: list[str] = []
    monkeypatch.setattr(credentials, "stream_login", lambda _f: credentials.StreamLogin(
        gs_token="gs", base_uri="https://b", refresh_token="rt"))
    monkeypatch.setattr(XcloudApi, "title_id", lambda _self, pid: "DEATHLOOP")
    monkeypatch.setattr(browser, "browser_cmd", lambda _edge: (["chrome"], "chrome"))
    monkeypatch.setattr(browser, "launch", lambda _cmd, _url: SimpleNamespace(
        poll=lambda: 0, wait=lambda timeout=None: 0))

    async def never_closes(_proc: Any) -> None:
        await asyncio.sleep(3600)

    async def stop(self: PlayerServer) -> None:
        ended.append("stopped")

    monkeypatch.setattr(xcloud_player_game, "_wait", never_closes)
    monkeypatch.setattr(xcloud_player_game, "_end", lambda _proc: ended.append("window"))
    monkeypatch.setattr(PlayerServer, "stop", stop)
    task = asyncio.create_task(xcloud_player_game.try_own_player(_svc(), _CTX, _URL))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended == ["window", "stopped"]


async def test_a_stream_uses_the_own_player_and_never_opens_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.core.types import Result
    from unifideck.services.launcher import browser_game

    edge_launches: list[str] = []

    async def own(*_a: Any) -> Result:
        return Result(success=True, store="microsoft")

    async def no_stage(*_a: Any, **_k: Any) -> None:
        return None

    async def emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(xcloud_player_game, "try_own_player", own)
    monkeypatch.setattr(browser_game, "emit_stage", no_stage)
    svc = SimpleNamespace(
        _edge_browser=SimpleNamespace(
            is_installed=True, process=None,
            launch_browser_game=lambda url, **_k: edge_launches.append(url) or True),
        _bus=SimpleNamespace(emit=emit), _config=None, _active_subprocess=None)
    ctx = SimpleNamespace(browser_url=_URL, browser_kind="stream", store="microsoft",
                          game_id="9P5Z4530318L", game_key="microsoft:9P5Z4530318L")
    result = await browser_game.run_browser_game(svc, ctx)  # type: ignore[arg-type]
    assert result.success and edge_launches == []


def test_closing_the_window_reaches_the_sandboxed_browser() -> None:
    """``flatpak run`` does not forward a signal into its sandbox: the whole group must go.

    Mirrors the measured shape: a leader that outlives nothing useful, plus a
    child (the browser) that would keep running after the leader is signalled.
    """
    import subprocess
    import time

    proc = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $! > /dev/stderr; wait"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    assert proc.stderr is not None
    child = int(proc.stderr.readline())
    time.sleep(0.2)
    started = time.monotonic()
    xcloud_player_game._end(proc)  # type: ignore[arg-type]
    # Inside the first 5 s window: the exited leader is reaped, not counted
    # alive as a zombie until the SIGKILL round runs out too.
    assert time.monotonic() - started < 4
    with pytest.raises(ProcessLookupError):
        import os
        os.kill(child, 0)
    proc.stderr.close()
