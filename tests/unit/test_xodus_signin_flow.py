"""The Xodus sign-in: ``XboxPcSignIn`` end to end with fakes.

A fake ``xodus-cli`` speaks the real ``NEXUS_AUTH_V1`` protocol over its
pipes. A fake page stands in for the Edge window: a navigation to an https
page answers with the page's ``external.notify`` call, or with the window
closing. The real ``XodusCli``, bridge and account check run in between.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _xodus_backend_fakes import install_backend

from unifideck.auth.edge_browser.cdp_session import CLOSED_EVENT
from unifideck.launcher.xbox_pc import backend
from unifideck.stores.microsoft.xodus import account_state
from unifideck.stores.microsoft.xodus import auth as auth_mod
from unifideck.stores.microsoft.xodus.auth import XboxPcSignIn
from unifideck.stores.microsoft.xodus.auth_protocol import BINDING
from unifideck.stores.microsoft.xodus.cli import XodusCli

LOGIN_SCRIPT = """
import json, os, sys
GAMERTAG = {gamertag!r}
MARK = {mark!r}
def frame(obj):
    print("NEXUS_AUTH_V1 " + json.dumps(obj), flush=True)
args = sys.argv[1:]
if args == ["--external-webview-stdio", "login"]:
    frame({{"type": "ready", "version": 1}})
    assert json.loads(sys.stdin.readline()) == {{"type": "hello", "version": 1}}
    frame({{"type": "open", "session": 1, "url": "https://login.live.com/ppsecure/InlineLogin.srf",
           "headers": {{"cxh-x": "1"}}}})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["type"] == "ipc":
            frame({{"type": "close", "session": 1}})
            sys.exit(0)
        if msg["type"] == "closed":
            sys.exit(2)
    sys.exit(3)
if args[0] == "account":
    print(json.dumps({{"stored": True, "username": "me@example.com"}}))
elif args[0] == "profile":
    print(json.dumps({{"schema": 1, "account": "me@example.com", "gamertag": GAMERTAG}}))
elif args[0] == "logout":
    open(MARK, "w").write("out")
"""


class FakeEdge:
    def __init__(self, alive: set[int] | None = None, installed: bool = True) -> None:
        self.cdp_port = 9222
        self.is_installed = installed
        self.alive = alive or set()
        self.prepared = 0
        self.closed = 0

    def browser_game_cdp_port(self) -> int:
        return 9223

    def storefront_cdp_port(self) -> int:
        return 9224

    def cdp_alive(self, port: int) -> bool:
        return port in self.alive

    async def prepare_auth_launch(self) -> None:
        self.prepared += 1

    async def close_auth_browser(self) -> bool:
        self.closed += 1
        return True


class FakePage:
    """``behaviour``: ``notify`` (the page signs in), ``close`` or ``idle``."""

    def __init__(self, behaviour: str = "notify", *, attach_ok: bool = True, delay: float = 0.0) -> None:
        self.behaviour = behaviour
        self.attach_ok = attach_ok
        self.delay = delay
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.posted: list[tuple[str, Any]] = []
        self.port: int | None = None

    async def attach(self, port: int, wait_s: float = 0) -> bool:
        await asyncio.sleep(self.delay)
        self.port = port
        return self.attach_ok

    async def post(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.posted.append((method, params))
        url = (params or {}).get("url", "")
        if method != "Page.navigate" or not url.startswith("https://"):
            return
        if self.behaviour == "notify":
            self.events.put_nowait({"method": "Page.frameNavigated", "params": {"frame": {"url": url}}})
            self.events.put_nowait({"method": "Runtime.bindingCalled",
                                    "params": {"name": BINDING, "payload": '{"type":"x"}'}})
        elif self.behaviour == "close":
            self.events.put_nowait({"method": CLOSED_EVENT})

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        return None


class FakeTokens:
    def __init__(self, *, loaded: bool = True, gamertag: str | None = "Gamer One") -> None:
        self.loaded = loaded
        self.gamertag = gamertag

    async def load(self) -> bool:
        return self.loaded

    async def build_chain(self) -> Any:
        return None if self.gamertag is None else type("Chain", (), {"gamertag": self.gamertag})()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    url_file = tmp_path / "ms_auth_url.txt"
    monkeypatch.setattr(auth_mod, "MS_AUTH_URL_FILE", str(url_file))
    backend._DIGEST_CACHE.clear()
    return {"url": url_file, "mark": tmp_path / "logout.mark"}


def _make(
    env: dict[str, Path], *, gamertag: str = "gamer one", page: FakePage | None = None,
    edge: FakeEdge | None = None, tokens: FakeTokens | None = None, busy: bool = False,
    install: bool = True,
) -> tuple[XboxPcSignIn, XodusCli, FakeEdge, FakePage, list[int]]:
    if install:
        install_backend(LOGIN_SCRIPT.format(gamertag=gamertag, mark=str(env["mark"])))
    cli = XodusCli(env_base={"PATH": "/usr/bin:/bin"})
    fake_edge = edge or FakeEdge()
    fake_page = page or FakePage()
    changes: list[int] = []
    signin = XboxPcSignIn(
        cli, lambda: fake_edge, tokens or FakeTokens(),  # type: ignore[arg-type]
        lambda: busy, lambda: changes.append(1),
        page_factory=lambda: fake_page,  # type: ignore[arg-type,return-value]
    )
    return signin, cli, fake_edge, fake_page, changes


async def _finish(signin: XboxPcSignIn) -> None:
    for _ in range(200):
        if not signin.running:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("sign-in did not finish")


async def test_happy_path_verifies_and_cleans_up(env: dict[str, Path]) -> None:
    signin, cli, edge, page, changes = _make(env)
    result = await signin.start()
    assert result == {"pending": True, "launch_shortcut": "microsoft", "reason": None}
    assert env["url"].read_text() == "about:blank"
    assert signin.running and cli.credential_lock.locked()
    await _finish(signin)
    assert signin.last_error is None
    assert page.port == 9222
    assert ("Page.navigate", {"url": "https://login.live.com/ppsecure/InlineLogin.srf"}) in page.posted
    assert edge.closed == 1 and not env["url"].exists()
    assert not cli.credential_lock.locked()
    saved = account_state.read_state()
    assert saved is not None and saved.gamertag == "gamer one" and not saved.mismatch
    assert len(changes) >= 2


async def test_gamertag_mismatch_signs_xodus_out(env: dict[str, Path]) -> None:
    signin, *_ = _make(env, gamertag="Someone Else")
    await signin.start()
    await _finish(signin)
    assert signin.last_error == "account_mismatch"
    assert env["mark"].read_text() == "out"
    saved = account_state.read_state()
    assert saved is not None and saved.mismatch


async def test_unreadable_gamertag_is_unverified_not_an_error(env: dict[str, Path]) -> None:
    signin, *_ = _make(env, tokens=FakeTokens(gamertag=None))
    await signin.start()
    await _finish(signin)
    assert signin.last_error is None
    saved = account_state.read_state()
    assert saved is not None and saved.unverified and saved.gamertag == "gamer one"
    assert account_state.needs_recheck(saved, now=saved.verified_at + 13 * 3600)
    assert not account_state.needs_recheck(saved, now=saved.verified_at + 60)


async def test_window_never_opens(env: dict[str, Path]) -> None:
    signin, cli, *_ = _make(env, page=FakePage(attach_ok=False))
    await signin.start()
    await _finish(signin)
    assert signin.last_error == "window_not_opened"
    assert not cli.credential_lock.locked() and not env["url"].exists()


async def test_window_closed_by_the_user(env: dict[str, Path]) -> None:
    signin, *_ = _make(env, page=FakePage("close"))
    await signin.start()
    await _finish(signin)
    assert signin.last_error == "window_closed"


async def test_open_before_attach_is_replayed(env: dict[str, Path]) -> None:
    page = FakePage(delay=0.4)
    signin, *_ = _make(env, page=page)
    await signin.start()
    await _finish(signin)
    assert signin.last_error is None
    assert any(m == "Page.navigate" for m, _ in page.posted)


async def test_timeout(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_mod, "SIGNIN_TIMEOUT_S", 0.5)
    signin, cli, *_ = _make(env, page=FakePage("idle"))
    await signin.start()
    await _finish(signin)
    assert signin.last_error == "signin_timeout"
    assert not cli.credential_lock.locked()


async def test_cancel_is_silent(env: dict[str, Path]) -> None:
    signin, cli, edge, *_ = _make(env, page=FakePage("idle"))
    await signin.start()
    await signin.cancel()
    assert not signin.running and signin.last_error is None
    assert not cli.credential_lock.locked() and edge.closed == 1


@pytest.mark.parametrize("kwargs, reason", [
    ({"busy": True}, "auth_in_progress"),
    ({"edge": FakeEdge(alive={9223})}, "edge_busy"),
    ({"edge": FakeEdge(alive={9224})}, "edge_busy"),
    ({"edge": FakeEdge(installed=False)}, "edge_not_installed"),
    ({"tokens": FakeTokens(loaded=False)}, "microsoft_sign_in_required"),
    ({"install": False}, "backend_missing"),
])
async def test_refusals(env: dict[str, Path], kwargs: dict[str, Any], reason: str) -> None:
    signin, cli, *_ = _make(env, **kwargs)
    result = await signin.start()
    assert result == {"pending": False, "launch_shortcut": None, "reason": reason}
    assert not signin.running and not cli.credential_lock.locked()
    assert not env["url"].exists()


async def test_second_start_while_running_is_refused(env: dict[str, Path]) -> None:
    signin, *_ = _make(env, page=FakePage("idle"))
    await signin.start()
    assert (await signin.start())["reason"] == "auth_in_progress"
    await signin.cancel()


async def test_leftover_auth_window_is_closed_first(env: dict[str, Path]) -> None:
    edge = FakeEdge(alive={9222})
    signin, *_ = _make(env, edge=edge)
    await signin.start()
    assert edge.prepared == 1
    await _finish(signin)


async def test_connect_needs_no_earlier_microsoft_sign_in(env: dict[str, Path]) -> None:
    """The one sign-in: with Xbox PC on, this sign-in is the Microsoft sign-in."""
    signin, *_ = _make(env, tokens=FakeTokens(loaded=False))
    assert (await signin.start())["reason"] == "microsoft_sign_in_required"
    result = await signin.start(connect=True)
    assert result == {"pending": True, "launch_shortcut": "microsoft", "reason": None}
    await _finish(signin)
