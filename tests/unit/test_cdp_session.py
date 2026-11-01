"""``CdpPageSession``: one CDP page socket that keeps every event.

A fake websocket stands in for Edge. It answers calls only when the test
says so, which is how "``post`` never waits" and "a reply never blocks the
events around it" are pinned (I-011).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from unifideck.auth.edge_browser.cdp_session import CLOSED_EVENT, CdpError, CdpPageSession


class FakeSocket:
    """An async-iterable socket. ``feed`` delivers a message to the reader."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def feed(self, msg: dict[str, Any] | None) -> None:
        self._incoming.put_nowait(None if msg is None else json.dumps(msg))

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self.feed(None)


def _session(sock: FakeSocket, targets: list[dict[str, Any]] | None = None) -> CdpPageSession:
    async def connect(url: str) -> FakeSocket:
        assert url == "ws://page"
        return sock

    found = targets if targets is not None else [
        {"type": "service_worker", "webSocketDebuggerUrl": "ws://worker"},
        {"type": "page", "webSocketDebuggerUrl": "ws://page"},
    ]
    return CdpPageSession(connect=connect, list_targets=lambda port: found)


async def test_attach_picks_the_page_target() -> None:
    sock = FakeSocket()
    page = _session(sock)
    assert await page.attach(9222, wait_s=1)
    assert page.connected
    await page.close()


async def test_attach_gives_up_without_a_page() -> None:
    page = _session(FakeSocket(), targets=[])
    assert not await page.attach(9222, wait_s=0.6)


async def test_post_does_not_wait_for_a_reply() -> None:
    sock = FakeSocket()
    page = _session(sock)
    await page.attach(9222, wait_s=1)
    await asyncio.wait_for(page.post("Page.navigate", {"url": "https://login.live.com/"}), 1)
    assert sock.sent[-1]["method"] == "Page.navigate"
    await page.close()


async def test_call_resolves_while_events_keep_flowing() -> None:
    sock = FakeSocket()
    page = _session(sock)
    await page.attach(9222, wait_s=1)
    call = asyncio.create_task(page.call("Runtime.evaluate", {"expression": "1"}))
    await asyncio.sleep(0.05)
    msg_id = sock.sent[-1]["id"]
    sock.feed({"method": "Fetch.requestPaused", "params": {"requestId": "r1"}})
    sock.feed({"method": "Page.loadEventFired", "params": {}})
    sock.feed({"id": msg_id, "result": {"result": {"value": 1}}})
    assert await asyncio.wait_for(call, 1) == {"result": {"value": 1}}
    first = await page.events.get()
    second = await page.events.get()
    assert [first["method"], second["method"]] == ["Fetch.requestPaused", "Page.loadEventFired"]
    await page.close()


async def test_call_error_and_timeout() -> None:
    sock = FakeSocket()
    page = _session(sock)
    await page.attach(9222, wait_s=1)
    call = asyncio.create_task(page.call("Fetch.enable"))
    await asyncio.sleep(0.05)
    sock.feed({"id": sock.sent[-1]["id"], "error": {"message": "nope"}})
    with pytest.raises(CdpError, match="nope"):
        await call
    with pytest.raises(CdpError, match="timed out"):
        await page.call("Page.enable", timeout_s=0.1)
    await page.close()


async def test_peer_close_queues_closed_and_fails_waiters() -> None:
    sock = FakeSocket()
    page = _session(sock)
    await page.attach(9222, wait_s=1)
    call = asyncio.create_task(page.call("Page.enable"))
    await asyncio.sleep(0.05)
    sock.feed(None)
    with pytest.raises(CdpError):
        await asyncio.wait_for(call, 1)
    assert (await asyncio.wait_for(page.events.get(), 1))["method"] == CLOSED_EVENT
    assert not page.connected
    with pytest.raises(CdpError):
        await page.post("Page.navigate")
    await page.close()
