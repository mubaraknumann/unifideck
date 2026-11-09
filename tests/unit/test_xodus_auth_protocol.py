"""The Xodus sign-in protocol and the page bridge.

``auth_protocol`` is pure. ``CxhPageBridge`` is driven with a fake page
that records what is posted or called, so the header merge, the
``post.srf`` relay and the trusted-page rule are pinned without an Edge.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from unifideck.stores.microsoft.xodus import auth_protocol as proto
from unifideck.stores.microsoft.xodus.auth_page import CxhPageBridge

INLINE = "https://login.live.com/ppsecure/InlineLogin.srf?flow=SECRET"
POST = "https://login.live.com/ppsecure/post.srf?code=SECRET"


def test_parse_frame() -> None:
    assert proto.parse_frame('NEXUS_AUTH_V1 {"type":"ready","version":1}\n') == {"type": "ready", "version": 1}
    for line in ("INFO hello", "NEXUS_AUTH_V1 {broken", 'NEXUS_AUTH_V1 ["list"]', "NEXUS_AUTH_V1 {}"):
        assert proto.parse_frame(line) is None


def test_host_frames_encode_one_line() -> None:
    raw = proto.encode(proto.ipc_frame(3, "payload"))
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw) == {"type": "ipc", "session": 3, "payload": "payload"}
    assert proto.hello_frame() == {"type": "hello", "version": 1}
    assert proto.closed_frame(2) == {"type": "closed", "session": 2}


@pytest.mark.parametrize("url, trusted", [
    ("https://login.live.com/x", True),
    ("https://account.microsoft.com/", True),
    ("http://login.live.com/x", False),
    ("https://login.live.com:444/x", False),
    ("https://evil.example/login.live.com", False),
    ("not a url", False),
])
def test_trusted_urls(url: str, trusted: bool) -> None:
    assert proto.is_trusted_url(url) is trusted


def test_safe_url_drops_the_query() -> None:
    assert proto.safe_url(POST) == "https://login.live.com/ppsecure/post.srf"
    assert "SECRET" not in proto.safe_url("about:blank?SECRET=1")


def test_cxh_headers_only_on_inline_login() -> None:
    pending = {"cxh-a": "1"}
    assert proto.merge_cxh_headers("https://login.live.com/ppsecure/other.srf", {}, pending) is None
    assert proto.merge_cxh_headers(INLINE, {}, None) is None
    merged = proto.merge_cxh_headers(INLINE, {"Accept": "x"}, pending)
    assert merged == [{"name": "Accept", "value": "x"}, {"name": "cxh-a", "value": "1"}]


def test_bridge_js_is_scoped() -> None:
    assert proto.BINDING in proto.BRIDGE_JS
    assert "window !== window.top" in proto.BRIDGE_JS
    assert all(host in proto.BRIDGE_JS for host in proto.TRUSTED_HOSTS)
    assert "%s" not in proto.BRIDGE_JS and "__HOSTS__" not in proto.BRIDGE_JS


def test_context_and_serverdata_and_notify_summary() -> None:
    expr = proto.context_dispatch_expr({"k": 1})
    assert expr.startswith('globalThis["CloudExperienceHost.Bridge.dispatchMessage"](')
    assert "CloudExperienceHost.getContext" in expr
    assert proto.serverdata_ok('{"a":1}') and not proto.serverdata_ok("null")
    assert not proto.serverdata_ok("x" * (proto.MAX_PAYLOAD + 1))
    assert proto.describe_notify(json.dumps({"sDAToken": "SECRET"})) == "token payload (redacted)"
    assert "SECRET" not in proto.describe_notify(json.dumps({"type": "SECRET" * 10}))


def test_describe_notify_shows_bridge_method_names() -> None:
    """A CXH method name is long enough for ``redact`` to hide it (I-061)."""
    name = "CloudExperienceHost.MSA.encryptUserData"
    msg = {"type": "invoke", "value": {"name": name, "args": ["hunter2"]}}
    out = proto.describe_notify(json.dumps(msg))
    assert out == f"type=invoke name={name}"
    assert "hunter2" not in out
    opaque = {"type": "invoke", "value": {"name": "A" * 60}}
    assert "A" * 40 not in proto.describe_notify(json.dumps(opaque))


class FakePage:
    def __init__(self, server_data: Any = '{"ok":1}') -> None:
        self.posted: list[tuple[str, dict[str, Any] | None]] = []
        self.called: list[str] = []
        self._server_data = server_data

    async def post(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.posted.append((method, params))

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.called.append(method)
        if method == "Runtime.evaluate":
            return {"result": {"value": self._server_data}}
        return {}


async def _bridge(page: FakePage) -> tuple[CxhPageBridge, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        sent.append(frame)

    bridge = CxhPageBridge(page, send)  # type: ignore[arg-type]
    await bridge.setup()
    return bridge, sent


async def _navigate(bridge: CxhPageBridge, url: str) -> None:
    await bridge.on_event({"method": "Page.frameNavigated", "params": {"frame": {"url": url}}})


async def test_setup_turns_on_interception_last() -> None:
    page = FakePage()
    await _bridge(page)
    assert page.called[-1] == "Fetch.enable"
    assert "Runtime.addBinding" in page.called and "Network.setUserAgentOverride" in page.called


async def test_open_navigates_and_adds_headers_once() -> None:
    page = FakePage()
    bridge, _ = await _bridge(page)
    await bridge.on_cli_frame({"type": "open", "session": 1, "url": INLINE, "headers": {"cxh-a": "1"}})
    assert page.posted[-1] == ("Page.navigate", {"url": INLINE})
    paused = {"method": "Fetch.requestPaused",
              "params": {"requestId": "r1", "request": {"url": INLINE, "headers": {}}}}
    await bridge.on_event(paused)
    assert page.posted[-1][1] == {"requestId": "r1", "headers": [{"name": "cxh-a", "value": "1"}]}
    await bridge.on_event({**paused, "params": {**paused["params"], "requestId": "r2"}})
    assert page.posted[-1][1] == {"requestId": "r2"}


async def test_open_refuses_non_https() -> None:
    page = FakePage()
    bridge, _ = await _bridge(page)
    await bridge.on_cli_frame({"type": "open", "session": 1, "url": "file:///etc/passwd"})
    assert bridge.session is None and not page.posted


async def test_post_srf_relays_serverdata() -> None:
    page = FakePage()
    bridge, sent = await _bridge(page)
    await bridge.on_cli_frame({"type": "open", "session": 4, "url": INLINE})
    await _navigate(bridge, POST)
    await bridge.on_event({"method": "Page.loadEventFired", "params": {}})
    assert sent == [{"type": "ipc", "session": 4, "payload": '{"ok":1}'}]


async def test_notify_only_from_trusted_pages_in_a_session() -> None:
    page = FakePage()
    bridge, sent = await _bridge(page)
    notify = {"method": "Runtime.bindingCalled", "params": {"name": proto.BINDING, "payload": "{}"}}
    await bridge.on_event(notify)
    assert sent == []
    await bridge.on_cli_frame({"type": "open", "session": 2, "url": INLINE})
    await _navigate(bridge, "https://evil.example/")
    await bridge.on_event(notify)
    assert sent == []
    await _navigate(bridge, "https://login.live.com/ppsecure/x")
    await bridge.on_event(notify)
    assert sent == [{"type": "ipc", "session": 2, "payload": "{}"}]


async def test_context_and_close() -> None:
    page = FakePage()
    bridge, _ = await _bridge(page)
    await bridge.on_cli_frame({"type": "open", "session": 5, "url": INLINE})
    await bridge.on_cli_frame({"type": "context", "session": 5, "context": {"c": 1}})
    assert page.posted[-1][0] == "Runtime.evaluate"
    await bridge.on_cli_frame({"type": "close", "session": 5})
    assert bridge.session is None
    assert page.posted[-1] == ("Page.navigate", {"url": "about:blank"})
