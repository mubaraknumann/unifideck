from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from unifideck.utils.config_helpers import get_cfg

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from unifideck.config import ConfigManager
def _pick_target_rank(target: dict[str, Any], substring: str) -> int:
    """Priority of a CEF page target for the library-UI pick (0 = unusable).

    Multiple page targets share the ``steamloopback.host`` marker on
    modern Steam builds (the ``SharedJSContext`` helper tab + the real
    ``Steam Big Picture Mode`` window). ``SharedJSContext`` hosts the
    helper ``document`` and ``querySelectorAll('button')`` returns 0
    there, so it is the lowest-priority match.

    4 = BPM window (by title); 3 = steamui ``/index.html`` URL (title
    may differ); 2 = substring match excluding ``SharedJSContext``;
    1 = any substring match (last resort — better than failing).
    """
    title = target.get("title")
    if title == "Steam Big Picture Mode":
        return 4
    if "steamloopback.host/index.html" in target.get("url", ""):
        return 3
    if not substring or substring in target.get("url", ""):
        return 1 if title == "SharedJSContext" else 2
    return 0


class CDPClient:
    """Cdpclient."""
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        """Initialize the instance."""
        self._host = host or get_cfg(config, "cdp.host", "127.0.0.1")
        self._port = port or int(get_cfg(config, "cdp.port", 8080))
        self._eval_timeout = float(
            get_cfg(config, "cdp.eval_timeout_seconds", 30)
        )
        self._response_timeout = float(
            get_cfg(config, "cdp.response_timeout_seconds", 10)
        )
        self._ws: Any = None  # WebSocketClientProtocol | None (annotated Any to avoid eager websockets import)
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._recv_task: asyncio.Task[Any] | None = None
        # Serialise eval calls so concurrent callers can't race on the
        # message-id <-> response mapping (staging used the same lock).
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Whether a CDP WebSocket connection is currently open.

        Also returns False if the receive loop has exited (which marks the
        socket dead by clearing ``self._ws``), so callers can rely on this
        as a "should I reconnect?" check.
        """
        return self._ws is not None

    async def connect(self, target_url_substring: str = "") -> bool:
        """Connect."""
        targets = await self._list_targets()
        target = self._pick_target(targets, target_url_substring)
        if not target or "webSocketDebuggerUrl" not in target:
            logger.warning(
                "[CDPClient] no suitable target among %d pages "
                "(substring=%r); titles=%s",
                len(targets), target_url_substring,
                [t.get("title", "?") for t in targets if t.get("type") == "page"],
            )
            return False
        try:
            import websockets
            self._ws = await websockets.connect(
                target["webSocketDebuggerUrl"],
                max_size=None,
            )
        except Exception:
            logger.exception("[CDPClient] ws connect failed")
            return False
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info(
            "[CDPClient] connected to target title=%r url=%s",
            target.get("title", "?"),
            target.get("url", "?")[:120],
        )
        return True
    async def disconnect(self) -> None:
        """Disconnect."""
        if self._recv_task:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def inject_css(self, css: str) -> bool:

        """Inject CSS."""
        expression = (
            "(() => {"
            "const s = document.createElement('style');"
            f"s.textContent = {json.dumps(css)};"
            "document.head.appendChild(s);"
            "return true; })()"
        )
        result = await self.evaluate(expression)
        return bool(result and result.get("result", {}).get("value"))
    async def evaluate(self, expression: str) -> dict[str, Any] | None:
        """Evaluate."""
        # ``userGesture`` matches staging — some Steam UI APIs gate on
        # whether the eval is treated as user-initiated. ``awaitPromise``
        # is explicitly False because our hide JS is synchronous; setting
        # it True would make CDP wait on every returned value, including
        # non-promises, which can stall.
        return await self._send("Runtime.evaluate", {
            "expression": expression,
            "userGesture": True,
            "awaitPromise": False,
            "returnByValue": True,
        })
    async def eval_js(self, expression: str) -> Any:
        """Eval js."""
        result = await self.evaluate(expression)
        if not result:
            return None
        # Surface JS exceptions instead of silently returning None — a JS
        # throw inside the evaluated expression otherwise looks identical
        # to "no WebSocket response" and we'd never know why a call
        # silently fails.
        inner = result.get("result", {})
        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            desc = (
                exc.get("exception", {}).get("description")
                or exc.get("text")
                or "unknown JS exception"
            )
            logger.warning("[CDPClient] JS exception: %s", desc)
            return None
        return inner.get("value")
    async def list_targets(self) -> list[dict[str, Any]]:
        """List targets."""
        return await self._list_targets()
    async def close_target(self, target_id: str) -> bool:
        """Close target."""
        if not target_id:
            return False
        import aiohttp
        url = (
            f"http://{self._host}:{self._port}"
            f"/json/close/{target_id}"
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp,
            ):
                return resp.status == 200
        except Exception as e:
            logger.debug(
                "[CDPClient] close_target failed: %s", e,
            )
            return False
    async def navigate(self, url: str) -> bool:
        """Navigate."""
        result = await self._send("Page.navigate", {"url": url})
        return result is not None
    async def wait_for_url(self, substring: str,
                           timeout: float | None = None) -> str | None:  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
        """Wait for URL."""
        deadline = asyncio.get_event_loop().time() + (
            timeout
            if timeout is not None
            else self._eval_timeout
        )
        while asyncio.get_event_loop().time() < deadline:
            result = await self.evaluate(
                "window.location.href",
            )
            if result:
                url = (
                    result.get("result", {}).get("value")
                    or ""
                )
                if substring in url:
                    return url
            await asyncio.sleep(0.5)
        return None

    async def _list_targets(self) -> list[dict[str, Any]]:

        """List targets."""
        import aiohttp
        url = f"http://{self._host}:{self._port}/json"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp,
            ):
                if resp.status != 200:
                    return []
                return cast("list[dict[str, Any]]", await resp.json())
        except Exception as e:
            logger.debug(
                "[CDPClient] list targets failed: %s", e,
            )
            return []
    @staticmethod
    def _pick_target(targets: list[dict[str, Any]],
                     substring: str) -> dict[str, Any] | None:
        """Pick the Steam library UI tab.

        Multiple CEF page targets share the ``steamloopback.host``
        URL marker on modern Steam builds (the ``SharedJSContext``
        helper tab + the actual ``Steam Big Picture Mode`` window).
        ``SharedJSContext`` does NOT host the visible action-bar DOM
        — its ``document`` is the helper context and
        ``querySelectorAll('button')`` returns 0 results there. So we
        explicitly prefer ``Steam Big Picture Mode`` (or a steamui
        ``/index.html`` URL with the on-deck flag) before falling
        back to any ``steamloopback.host`` match. The fallback keeps
        us alive on older Steam builds where only one tab matched.
        """
        pages = [t for t in targets if t.get("type") == "page"]
        # Highest-priority page wins; ties resolve to the first in
        # document order. See ``_pick_target_rank`` for the priorities.
        best: dict[str, Any] | None = None
        best_rank = 0
        for t in pages:
            rank = _pick_target_rank(t, substring)
            if rank > best_rank:
                best, best_rank = t, rank
        return best
    async def _send(self, method: str,
                    params: dict[str, Any]) -> dict[str, Any] | None:
        """Send."""
        if not self._ws:
            return None
        async with self._send_lock:
            if not self._ws:
                return None
            self._request_id += 1
            req_id = self._request_id
            message = {
                "id": req_id,
                "method": method,
                "params": params,
            }
            future: asyncio.Future[Any] = (
                asyncio.get_event_loop().create_future()
            )
            self._pending[req_id] = future
            try:
                await self._ws.send(json.dumps(message))
                return await asyncio.wait_for(
                    future, timeout=self._response_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "[CDPClient] timeout on %s — marking disconnected",
                    method,
                )
                # Force reconnect on next call (staging parity).
                self._mark_disconnected()
                return None
            except Exception as e:
                # WS likely dropped mid-send ("no close frame received or
                # sent"). Drop the dead socket so the next ``connect()``
                # establishes a fresh one.
                logger.warning(
                    "[CDPClient] send failed on %s: %s — marking disconnected",
                    method,
                    e,
                )
                self._mark_disconnected()
                return None
            finally:
                self._pending.pop(req_id, None)

    def _mark_disconnected(self) -> None:
        """Drop the WS handle + fail pending futures so callers retry."""
        self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("CDP connection lost"))
        self._pending.clear()
    async def _recv_loop(self) -> None:
        """Recv loop.

        Marks the client disconnected when the loop exits (clean close
        OR error) so the next ``connect()`` reconnects from scratch.
        Without this, a dropped socket silently stays "connected"
        forever and every later RPC fails with the same close-frame
        error.
        """
        if self._ws is None:
            logger.warning(
                "[CDPClient] _recv_loop started before connect()",
            )
            return
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                req_id = msg.get("id")
                if req_id and req_id in self._pending:
                    self._pending[req_id].set_result(
                        msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[CDPClient] recv loop error: %s", e)
        finally:
            self._mark_disconnected()
