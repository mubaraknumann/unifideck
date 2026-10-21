"""stores/microsoft/xodus/auth_page.py — the sign-in page side of the bridge.

``CxhPageBridge`` drives the Edge window over CDP for one Xodus sign-in:

* ``setup`` turns on the CDP domains, sets the Windows web host user agent,
  adds the ``external.notify`` binding and pauses ``login.live.com``
  document requests so the backend's ``cxh-*`` headers can be added.
* ``on_cli_frame`` handles the backend's ``open``, ``close`` and
  ``context`` frames.
* ``on_event`` handles the page's CDP events: a paused request is
  continued (with the headers, on the first ``InlineLogin.srf``), a
  finished ``post.srf`` hands its ``ServerData`` to the backend, and a
  ``notify`` from a trusted page is relayed.

Navigation and request continuation are posted without waiting for a reply
(I-011). Only frame types, hosts and paths are logged.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from unifideck.auth.edge_browser.cdp_session import CdpError, CdpPageSession

from . import auth_protocol as proto

logger = logging.getLogger(__name__)

SendCli = Callable[[dict[str, Any]], Awaitable[None]]
_LOG = "[XboxPcSignIn]"


class CxhPageBridge:
    """Relays between one sign-in window and ``xodus-cli login``."""

    def __init__(self, page: CdpPageSession, send_cli: SendCli) -> None:
        """``send_cli`` writes one host frame to the CLI's stdin."""
        self._page = page
        self._send_cli = send_cli
        #: The backend's current web session, or None between sessions.
        self.session: int | None = None
        self._pending_headers: dict[str, str] | None = None
        self._main_url = ""
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "Fetch.requestPaused": self._on_request_paused,
            "Page.frameNavigated": self._on_frame_navigated,
            "Page.loadEventFired": self._on_load,
            "Runtime.bindingCalled": self._on_binding,
        }

    async def setup(self) -> None:
        """Prepare the page. Raises ``CdpError``."""
        page = self._page
        for method in ("Page.enable", "Runtime.enable", "Network.enable"):
            await page.call(method)
        await page.call("Network.setUserAgentOverride", {"userAgent": proto.USER_AGENT})
        await page.call("Runtime.addBinding", {"name": proto.BINDING})
        await page.call("Page.addScriptToEvaluateOnNewDocument", {"source": proto.BRIDGE_JS})
        await page.call("Fetch.enable", {"patterns": proto.FETCH_PATTERNS})

    # ── Frames from the backend ─────────────────────────────────────

    async def on_cli_frame(self, frame: dict[str, Any]) -> None:
        """Handle ``open``, ``close`` or ``context``."""
        kind = frame.get("type")
        if kind == "open":
            await self._open(frame)
        elif kind == "close":
            if frame.get("session") == self.session:
                logger.info("%s backend closed session %s", _LOG, self.session)
                self.session = None
                await self._page.post("Page.navigate", {"url": "about:blank"})
        elif kind == "context":
            if frame.get("session") == self.session:
                expr = proto.context_dispatch_expr(frame.get("context"))
                await self._page.post("Runtime.evaluate", {"expression": expr})
        else:
            logger.info("%s unexpected frame type %r", _LOG, kind)

    async def _open(self, frame: dict[str, Any]) -> None:
        url, session = frame.get("url"), frame.get("session")
        if not isinstance(url, str) or not isinstance(session, int) or not url.startswith("https://"):
            logger.warning("%s ignored an open frame without an https url", _LOG)
            return
        headers = frame.get("headers")
        self.session = session
        self._pending_headers = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else None
        logger.info("%s open session %s -> %s", _LOG, session, proto.safe_url(url))
        await self._page.post("Page.navigate", {"url": url})

    # ── Events from the page ────────────────────────────────────────

    async def on_event(self, msg: dict[str, Any]) -> None:
        """Handle one CDP event. Unknown events are ignored."""
        handler = self._handlers.get(str(msg.get("method")))
        params = msg.get("params")
        if handler is not None and isinstance(params, dict):
            await handler(params)

    async def _on_request_paused(self, params: dict[str, Any]) -> None:
        request = params.get("request") if isinstance(params.get("request"), dict) else {}
        out: dict[str, Any] = {"requestId": params.get("requestId")}
        page_headers = request.get("headers")
        headers = proto.merge_cxh_headers(
            str(request.get("url") or ""),
            page_headers if isinstance(page_headers, dict) else {},
            self._pending_headers,
        )
        if headers is not None:
            out["headers"] = headers
            logger.info("%s added %d sign-in headers", _LOG, len(self._pending_headers or {}))
            self._pending_headers = None
        await self._page.post("Fetch.continueRequest", out)

    async def _on_frame_navigated(self, params: dict[str, Any]) -> None:
        frame = params.get("frame")
        if isinstance(frame, dict) and not frame.get("parentId"):
            self._main_url = str(frame.get("url") or "")
            logger.info("%s page -> %s", _LOG, proto.safe_url(self._main_url))

    async def _on_load(self, _params: dict[str, Any]) -> None:
        if self.session is None or not proto.is_post_srf(self._main_url):
            return
        try:
            result = await self._page.call(
                "Runtime.evaluate", {"expression": proto.SERVER_DATA_EXPR, "returnByValue": True},
            )
        except CdpError as e:
            logger.warning("%s could not read the finished page: %s", _LOG, e)
            return
        value = (result.get("result") or {}).get("value")
        if proto.serverdata_ok(value):
            logger.info("%s sign-in page finished; relaying its result", _LOG)
            await self._send_cli(proto.ipc_frame(self.session, str(value)))

    async def _on_binding(self, params: dict[str, Any]) -> None:
        if params.get("name") != proto.BINDING:
            return
        payload = params.get("payload")
        if self.session is None or not proto.is_trusted_url(self._main_url) or not isinstance(payload, str):
            logger.info("%s page message ignored (no session or untrusted page)", _LOG)
            return
        logger.info("%s page message -> backend: %s", _LOG, proto.describe_notify(payload))
        await self._send_cli(proto.ipc_frame(self.session, payload))
