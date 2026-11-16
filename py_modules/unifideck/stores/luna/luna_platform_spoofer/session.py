"""The browser-level CDP session seam for the Luna platform spoofer.

``attach()`` drives a :class:`BrowserCdpSession`, not aiohttp directly, so the
whole auto-attach + per-target override loop can be exercised against a mock
session that emits a scripted set of targets and records per-target ordering
(property 1's "override before resume"). :class:`_AiohttpBrowserCdpSession` is
the only part that touches a live browser.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from unifideck.stores.luna.luna_platform_spoofer.payloads import (
    _ATTACH_IDLE_TIMEOUT_S,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class BrowserCdpSession(Protocol):
    """A browser-level CDP session: send commands, consume attach events.

    Commands are routed to a target by ``session_id`` (CDP "flatten" mode);
    ``session_id=None`` addresses the browser session itself (used for the
    browser-level ``Target.setAutoAttach``).
    """

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one CDP command, return its result payload."""
        ...

    def attached_targets(
        self, idle_timeout: float | None = ...,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(session_id, target_info)`` for each attached target.

        Ends after ``idle_timeout`` seconds without a new target; ``None``
        listens until the browser websocket closes.
        """
        ...


class _AiohttpBrowserCdpSession:
    """aiohttp-backed :class:`BrowserCdpSession` over the browser websocket.

    Mirrors the established CDP-over-websocket pattern in this codebase
    (``launcher/cdp/cdp_primitives.py`` / ``cdp/page_inject.py``): one
    long-lived ``aiohttp`` websocket to the browser endpoint, monotonically
    increasing message ids, ``sessionId`` routing for flatten mode.
    Command replies and ``Target.attachedToTarget`` events share the one
    socket, so :meth:`send` skips over events it is not waiting for and
    :meth:`attached_targets` buffers replies it is not the recipient of.
    """

    def __init__(self, ws_url: str) -> None:
        """Bind to a browser-level CDP websocket URL (not yet connected)."""
        self._ws_url = ws_url
        self._session: Any = None
        self._ws: Any = None
        self._next_id = 0
        self._pending_events: list[tuple[str, dict[str, Any]]] = []

    async def _ensure_ws(self) -> Any:
        """Open the websocket on first use (lazy connect)."""
        if self._ws is not None:
            return self._ws
        import aiohttp
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            self._ws_url, heartbeat=10, autoping=True,
        )
        return self._ws

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one CDP command and return its ``result`` payload.

        Routes to a target via ``sessionId`` when given (flatten mode).
        Interleaved ``Target.attachedToTarget`` events are buffered so a
        concurrent :meth:`attached_targets` consumer does not lose them.
        """
        ws = await self._ensure_ws()
        self._next_id += 1
        msg_id = self._next_id
        message: dict[str, Any] = {
            "id": msg_id, "method": method, "params": params or {},
        }
        if session_id is not None:
            message["sessionId"] = session_id
        await ws.send_json(message)
        while True:
            raw = await ws.receive(timeout=15)
            payload = self._decode_response_frame(raw)
            if payload is None:
                continue
            if payload.get("id") != msg_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"{method} failed: {payload['error']}")
            result = payload.get("result")
            return result if isinstance(result, dict) else {}

    def _decode_response_frame(self, raw: Any) -> dict[str, Any] | None:
        """Decode a CDP response frame for :meth:`send`.

        Returns the parsed payload, or ``None`` when the frame should be
        skipped (non-text keep-alive or a buffered attach event). Raises
        when the websocket has closed.
        """
        import aiohttp
        if raw.type != aiohttp.WSMsgType.TEXT:
            if raw.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                raise RuntimeError("CDP browser websocket closed")
            return None
        payload = json.loads(raw.data)
        if payload.get("method") == "Target.attachedToTarget":
            self._buffer_event(payload)
            return None
        return payload if isinstance(payload, dict) else {}

    def _buffer_event(self, payload: dict[str, Any]) -> None:
        """Record a ``Target.attachedToTarget`` event for later consumption."""
        params = payload.get("params") or {}
        session_id = params.get("sessionId")
        target_info = params.get("targetInfo") or {}
        if session_id:
            self._pending_events.append((session_id, target_info))

    async def attached_targets(
        self, idle_timeout: float | None = _ATTACH_IDLE_TIMEOUT_S,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(session_id, target_info)`` as targets attach.

        Drains any events buffered by :meth:`send` first, then reads the
        browser websocket for further ``Target.attachedToTarget`` events.

        Bounded by an **idle timeout**: ``Target.setAutoAttach`` delivers one
        ``attachedToTarget`` burst for the already-open targets, then the
        browser ws goes silent. An unbounded ``ws.receive()`` therefore blocks
        forever and ``run_attach`` never returns (an on-device hang).
        Ending the iteration once no further target has attached for
        :data:`_ATTACH_IDLE_TIMEOUT_S` lets the spoofer finish the setup loop
        and the caller proceed, while still covering every target the burst
        delivered.

        ``idle_timeout=None`` keeps listening until the websocket closes (the
        browser exited): the spoofer's keep-alive loop uses it to service
        targets that open later in the session. Events buffered by a
        :meth:`send` issued from the loop body are drained on every pass.
        """

        import aiohttp
        ws = await self._ensure_ws()
        while True:
            while self._pending_events:
                yield self._pending_events.pop(0)
            try:
                raw = await ws.receive(timeout=idle_timeout)
            except TimeoutError:
                # No further target attached within the idle window — the
                # auto-attach burst is done. End setup rather than block.
                return
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    return
                continue
            payload = json.loads(raw.data)
            if payload.get("method") != "Target.attachedToTarget":
                continue
            params = payload.get("params") or {}
            session_id = params.get("sessionId")
            target_info = params.get("targetInfo") or {}
            if session_id:
                yield session_id, target_info

    async def close(self) -> None:
        """Close the websocket and its aiohttp session."""
        import contextlib
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
