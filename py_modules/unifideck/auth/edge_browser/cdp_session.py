"""auth.edge_browser.cdp_session — one CDP page session that keeps its events.

``cdp/cdp_client.CDPClient`` drops events and holds its send lock while it
waits for a reply. A sign-in that intercepts requests needs the opposite:

* every event is queued, in order, for the caller to handle;
* ``post`` sends without waiting for the reply. A ``Page.navigate`` whose
  reply is awaited deadlocks against the request it pauses (I-011);
* ``call`` takes the lock only to send, then waits for its reply outside it,
  so a reply and the events around it never block each other.

When the socket closes (the user closed the window), a
``{"method": "__closed__"}`` event is queued and every waiting ``call``
fails with ``CdpError``.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .cdp_client import EdgeCDPClient

logger = logging.getLogger(__name__)

#: The event queued when the page's socket closes.
CLOSED_EVENT = "__closed__"
ATTACH_WAIT_S = 120.0
_ATTACH_POLL_S = 0.5
CALL_TIMEOUT_S = 30.0

ConnectFn = Callable[[str], Awaitable[Any]]
ListTargetsFn = Callable[[int], list[dict[str, Any]]]


class CdpError(RuntimeError):
    """A CDP call failed, timed out, or the socket is gone."""


async def _default_connect(url: str) -> Any:
    import websockets
    return await websockets.connect(url, max_size=None, ping_interval=None)


def _default_list_targets(port: int) -> list[dict[str, Any]]:
    return EdgeCDPClient(cdp_port=port).list_targets()


class CdpPageSession:
    """A websocket to the first page target of an Edge on one CDP port."""

    def __init__(
        self,
        *,
        connect: ConnectFn | None = None,
        list_targets: ListTargetsFn | None = None,
    ) -> None:
        """Both hooks exist for tests; the defaults talk to a real Edge."""
        self._connect = connect or _default_connect
        self._list_targets = list_targets or _default_list_targets
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        #: Every CDP event, in arrival order, ending with ``__closed__``.
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @property
    def connected(self) -> bool:
        """Whether the socket is open."""
        return self._reader is not None and not self._reader.done()

    async def _first_page_url(self, port: int, wait_s: float) -> str | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            targets = await asyncio.to_thread(self._list_targets, port)
            page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            if page is not None:
                return str(page["webSocketDebuggerUrl"])
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(_ATTACH_POLL_S)

    async def attach(self, port: int, wait_s: float = ATTACH_WAIT_S) -> bool:
        """Wait up to ``wait_s`` for a page on ``port`` and connect to it."""
        url = await self._first_page_url(port, wait_s)
        if url is None:
            return False
        try:
            self._ws = await self._connect(url)
        except Exception as e:
            logger.warning("[CdpPageSession] could not connect to the page: %s", type(e).__name__)
            return False
        self._reader = asyncio.create_task(self._read_loop(), name="cdp-page-reader")
        return True

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                self._dispatch(raw)
        except Exception as e:
            logger.info("[CdpPageSession] socket ended: %s", type(e).__name__)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("socket closed"))
            self._pending.clear()
            self.events.put_nowait({"method": CLOSED_EVENT})

    def _dispatch(self, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if isinstance(msg_id, int):
            future = self._pending.pop(msg_id, None)
            if future is not None and not future.done():
                future.set_result(msg)
            return
        self.events.put_nowait(msg)

    async def _send(self, msg_id: int, method: str, params: dict[str, Any] | None) -> None:
        if not self.connected:
            raise CdpError("not connected")
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        async with self._send_lock:
            await self._ws.send(payload)

    async def post(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send and do not wait for the reply (navigation, request continue)."""
        await self._send(next(self._ids), method, params)

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = CALL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send and wait for the reply's ``result``. Raises ``CdpError``."""
        msg_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._send(msg_id, method, params)
            reply = await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            raise CdpError(f"{method} timed out") from None
        finally:
            self._pending.pop(msg_id, None)
        error = reply.get("error")
        if isinstance(error, dict):
            raise CdpError(f"{method}: {error.get('message')}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        """Close the socket and stop the reader."""
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
