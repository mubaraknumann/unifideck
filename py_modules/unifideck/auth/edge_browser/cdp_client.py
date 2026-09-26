"""auth.edge_browser.cdp_client — Chrome DevTools Protocol client for Edge.

Extracted from edge_browser.py to isolate CDP-level concerns (the HTTP
protocol used to enumerate tabs, navigate, and close targets) from the
browser process lifecycle and the installer. This module knows nothing
about subprocess management — it only speaks to a running Edge
instance through its ``--remote-debugging-port=N`` endpoint.

The module is imported by ``EdgeBrowser`` which composes an
``EdgeCDPClient`` as ``self._cdp`` and delegates the four pure-CDP
methods through thin stubs, preserving the pre-split public API for
``list_targets``, ``_get_browser_ws_url``, ``navigate_tab``, and
``_close_all_cdp_targets``.

Responsibilities:
 - Probe the CDP /json/version endpoint (used for up/down checks)
 - List CDP targets (tabs + workers) via /json/list
 - Navigate a target to a URL via websocket (used after OAuth to
   visit xbox.com so session cookies land in the shared profile)
 - Close all CDP targets via /json/close/{id}

Reference: edge_browser.py pre-split, lines 430-598.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class EdgeCDPClient:
    """Pure-CDP client for a locally running Edge instance.

    Constructor takes only the CDP port number; everything else is
    derived from HTTP calls to ``http://127.0.0.1:{port}/json/*`` and
    websocket connections to target-specific URLs returned by the
    /json/list endpoint.

    Usage::

        cdp = EdgeCDPClient(cdp_port=9222)
        if cdp.probe_cdp():   # port is up
            await cdp.navigate_tab("https://xbox.com")
            await cdp.close_all_targets(log_prefix="auth")
    """

    def __init__(self, cdp_port: int) -> None:
        """Build a CDP client bound to the given debugging port."""
        self.cdp_port = cdp_port

    # ── Lightweight probes ───────────────────────────────────────────

    def get_browser_ws_url(self) -> str | None:
        """Return the live CDP browser websocket URL, if the browser is up."""
        import urllib.request as _req
        try:
            with _req.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/version",
                timeout=1,
            ) as r:
                data = json.loads(r.read().decode())
                ws_url = data.get("webSocketDebuggerUrl")
                return ws_url if ws_url else None
        except Exception:
            return None

    def probe_cdp(self) -> bool:
        """Blocking probe of /json/version — True if the browser answers."""
        import urllib.request
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/version",
                timeout=1,
            ):
                return True
        except Exception:
            return False

    def list_targets(self) -> list[dict[str, Any]]:
        """Return the current CDP targets exposed by the browser."""
        import urllib.request as _req
        try:
            with _req.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/list",
                timeout=1,
            ) as r:
                data = json.loads(r.read().decode())
                return data if isinstance(data, list) else []
        except Exception:
            return []

    # ── Navigation ───────────────────────────────────────────────────

    async def navigate_tab(
        self,
        url: str,
        timeout: float = 15.0,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    ) -> bool:
        """Navigate the first page target to *url* via CDP and wait for load.

        Used after OAuth to visit ``xbox.com`` so session cookies are
        established in the shared profile before the browser is closed.
        Returns ``True`` if navigation succeeded, ``False`` on any error.
        """
        targets = self.list_targets()
        page_target = next(
            (t for t in targets if t.get("type") == "page"), None,
        )
        if not page_target:
            logger.warning("[Edge] navigate_tab: no page target found")
            return False
        ws_url = page_target.get("webSocketDebuggerUrl")
        if not ws_url:
            logger.warning(
                "[Edge] navigate_tab: no webSocketDebuggerUrl",
            )
            return False
        try:
            import websockets
        except ImportError:
            logger.warning(
                "[Edge] navigate_tab: websockets not available",
            )
            return False

        try:
            async with websockets.connect(ws_url, close_timeout=3) as ws:
                # Enable Page events so we receive load notifications
                await ws.send(json.dumps({
                    "id": 1,
                    "method": "Page.enable",
                    "params": {},
                }))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=3)
                # Navigate
                await ws.send(json.dumps({
                    "id": 2,
                    "method": "Page.navigate",
                    "params": {"url": url},
                }))
                deadline = asyncio.get_event_loop().time() + timeout
                return await _await_navigation_result(
                    ws, deadline, url,
                )
        except Exception as exc:
            logger.warning("[Edge] navigate_tab failed: %s", exc)
            return False

    # ── Close all targets ────────────────────────────────────────────

    async def close_all_targets(self, *, log_prefix: str) -> bool:
        """Close all live targets exposed on this browser's CDP port.

        Uses the DevTools HTTP ``/json/close`` endpoint (rather
        than per-target WebSocket Page.close) because we want to
        slam the door even if a target's WebSocket is stuck.
        After successful closures we briefly poll for the browser
        WebSocket URL to drop, which is the cheapest "browser is
        gone" signal Chromium gives us. Returns True if at least
        one target was closed.
        """
        targets = self.list_targets()
        if not targets:
            return False

        closed_any = False
        for target in targets:
            target_id = target.get("id")
            if not target_id:
                continue
            if await self._close_one_target(target_id, log_prefix):
                closed_any = True

        if closed_any:
            await self._await_browser_ws_gone()
            logger.info(
                "[Edge] Closed %s browser targets via DevTools HTTP",
                log_prefix,
            )
        return closed_any

    async def _close_one_target(self, target_id: str, log_prefix: str) -> bool:
        """Issue one ``/json/close/<id>`` HTTP request, return True on success.

        404 is treated as "already gone" (success of a sort) and
        logged at debug level only; other HTTP errors and
        unexpected exceptions are warned about so ops can spot a
        truly stuck browser. Network I/O is dispatched via
        :func:`asyncio.to_thread` to keep the event loop free.
        """
        import urllib.error as _err
        import urllib.request as _req

        def _close_blocking() -> None:
            with _req.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/close/{target_id}",
                timeout=2,
            ) as r:
                r.read()

        try:
            await asyncio.to_thread(_close_blocking)
            return True
        except _err.HTTPError as e:
            if e.code == 404:
                logger.debug(
                    "[Edge] %s target %s already gone",
                    log_prefix, target_id,
                )
                return False
            logger.warning(
                "[Edge] Could not close %s target %s: %s",
                log_prefix, target_id, e,
            )
            return False
        except Exception as e:
            logger.warning(
                "[Edge] Could not close %s target %s: %s",
                log_prefix, target_id, e,
            )
            return False

    async def _await_browser_ws_gone(self) -> None:
        """Poll the browser WebSocket URL until it disappears (5 s budget).

        The browser usually drops within a second or two after all
        targets are closed; we wait up to 20 x 250 ms before giving
        up. Returning anyway is safe because the caller's intent is
        already satisfied — this is only a courtesy delay so the
        next launch doesn't race against the previous Chromium's
        shutdown.
        """
        for _ in range(20):
            await asyncio.sleep(0.25)
            if not self.get_browser_ws_url():
                return


async def _await_navigation_result(
    ws: Any, deadline: float, url: str,
) -> bool:
    """Poll a CDP websocket for ``Page.navigate`` + frame-stopped-loading.

    Returns True on successful navigation (id=2 ack without
    error + a Page.frameStoppedLoading or Page.loadEventFired
    event), or True if only the ack was seen before the
    deadline (cookies still end up set, just the full load
    event was missed). Returns False on error payload or
    deadline reached without ack.
    """
    got_navigate_ok = False
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=remaining,
            )
        except TimeoutError:
            break
        msg = json.loads(raw)
        # Response to our Page.navigate call.
        if msg.get("id") == 2:
            if "error" in msg:
                logger.warning(
                    "[Edge] navigate_tab error: %s",
                    msg["error"],
                )
                return False
            got_navigate_ok = True
        # Frame finished loading.
        if (
            msg.get("method") in (
                "Page.frameStoppedLoading",
                "Page.loadEventFired",
            )
            and got_navigate_ok
        ):
            logger.info(
                "[Edge] navigate_tab: loaded %s", url,
            )
            return True
    # Navigation started but page didn't fully load — still OK
    # for cookies.
    if got_navigate_ok:
        logger.info(
            "[Edge] navigate_tab: navigation sent, "
            "load timed out for %s", url,
        )
        return True
    return False
