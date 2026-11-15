"""Catch a sign-in redirect to a scheme the browser never opens.

EA's sign-in ends with a 302 to ``qrc:/html/login_successful.html?code=…``, a
scheme only the EA app understands. Chromium never commits that navigation (it
offers to hand it to the desktop instead), so the polling monitor never sees the
URL. This reads the ``Location`` header straight off the 302 over CDP ``Fetch``,
then answers the request with a local page, so the browser shows "signed in"
and nothing reaches the desktop's URL handlers.

The interception is browser-wide (CDP's browser target), not tied to one page:
a fresh EA sign-in can finish in another page target than the one it started
in, and a page-scoped interception then missed the redirect and Chromium asked
to "Open Maxima Launcher?" (2026-11-15). Only documents matching
``RedirectIntercept.url_pattern`` are paused, so other stores sharing the
sign-in browser's profile are not touched.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from .browser_types import AuthCaptureResult, RedirectIntercept
from .browser_url_parsing import build_redirect_capture
from .edge_browser.cdp_client import EdgeCDPClient
from .edge_browser.cdp_session import CLOSED_EVENT, CdpError, CdpPageSession

logger = logging.getLogger(__name__)

_ATTACH_RETRY_S = 1.0


def intercepted_capture(
    task: asyncio.Task[tuple[str, str] | None] | None, start: float,
) -> AuthCaptureResult | None:
    """The capture once :func:`capture_redirect` (run as ``task``) has one."""
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return None
    caught = task.result()
    if caught is None:
        return None
    location, page_url = caught
    capture = build_redirect_capture(location, start)
    capture.page_url = page_url
    return capture


def _location(params: dict[str, Any]) -> str:
    for header in params.get("responseHeaders") or []:
        if str(header.get("name", "")).lower() == "location":
            return str(header.get("value", ""))
    return ""


async def _attach(port: int) -> CdpPageSession:
    """Wait for the browser on ``port`` and connect to its browser target."""

    def browser_target(cdp_port: int) -> list[dict[str, Any]]:
        url = EdgeCDPClient(cdp_port=cdp_port).get_browser_ws_url()
        return [{"type": "page", "webSocketDebuggerUrl": url}] if url else []

    while True:
        session = CdpPageSession(list_targets=browser_target)
        if await session.attach(port, wait_s=_ATTACH_RETRY_S):
            return session
        await asyncio.sleep(_ATTACH_RETRY_S)


async def _answer(session: CdpPageSession, request_id: str, page: str) -> None:
    await session.post("Fetch.fulfillRequest", {
        "requestId": request_id,
        "responseCode": 200,
        "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
        "body": base64.b64encode(page.encode()).decode(),
    })


async def capture_redirect(port: int, intercept: RedirectIntercept) -> tuple[str, str] | None:
    """The redirect target, and the page that was sent there, once it happens.

    Runs until the redirect arrives (returns ``(location, request URL)``; the
    tab then shows ``done_page`` at that request URL) or the browser is gone
    (returns None). The caller cancels it when the flow ends another way.
    """
    while True:
        session = await _attach(port)
        try:
            await session.call("Fetch.enable", {"patterns": [{
                "urlPattern": intercept.url_pattern,
                "resourceType": "Document",
                "requestStage": "Response",
            }]})
        except CdpError as e:
            logger.info("[auth/intercept] could not intercept yet: %s", e)
            await session.close()
            continue
        logger.info("[auth/intercept] watching %s", intercept.url_pattern)
        try:
            while True:
                event = await session.events.get()
                if event.get("method") == CLOSED_EVENT:
                    return None  # the browser is gone
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event.get("params") or {}
                location = _location(params)
                if location.startswith(intercept.location_prefix):
                    await _answer(session, params["requestId"], intercept.done_page)
                    logger.info("[auth/intercept] captured the %s redirect", intercept.location_prefix)
                    return location, str((params.get("request") or {}).get("url", ""))
                await session.post("Fetch.continueRequest", {"requestId": params["requestId"]})
        finally:
            await session.close()
