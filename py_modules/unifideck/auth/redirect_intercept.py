"""Catch a sign-in redirect to a scheme the browser never opens.

EA's sign-in ends with a 302 to ``qrc:/html/login_successful.html?code=…``, a
scheme only the EA app understands. Chromium never commits that navigation (it
offers to hand it to the desktop instead), so the polling monitor never sees the
URL. This reads the ``Location`` header straight off the 302 over CDP ``Fetch``,
then answers the request with a local page, so the browser shows "signed in"
and nothing reaches the desktop's URL handlers.

Only the page whose URL contains ``RedirectIntercept.page_host`` is attached:
the sign-in browser's profile is shared by every store.
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


def intercepted_capture(task: asyncio.Task[str | None] | None, start: float) -> AuthCaptureResult | None:
    """The capture once :func:`capture_redirect` (run as ``task``) has one."""
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return None
    redirect = task.result()
    return build_redirect_capture(redirect, start) if redirect else None


def _location(params: dict[str, Any]) -> str:
    for header in params.get("responseHeaders") or []:
        if str(header.get("name", "")).lower() == "location":
            return str(header.get("value", ""))
    return ""


async def _attach(port: int, intercept: RedirectIntercept) -> CdpPageSession:
    """Wait for the sign-in page on ``port`` and connect to it."""

    def sign_in_pages(cdp_port: int) -> list[dict[str, Any]]:
        targets = EdgeCDPClient(cdp_port=cdp_port).list_targets()
        return [t for t in targets if intercept.page_host in str(t.get("url", ""))]

    while True:
        session = CdpPageSession(list_targets=sign_in_pages)
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


async def capture_redirect(port: int, intercept: RedirectIntercept) -> str | None:
    """The redirect target once the browser on ``port`` is sent there.

    Runs until the redirect arrives (returns its URL) or the window closes
    (returns None). The caller cancels it when the flow ends another way.
    """
    while True:
        session = await _attach(port, intercept)
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
                    return None
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event.get("params") or {}
                location = _location(params)
                if location.startswith(intercept.location_prefix):
                    await _answer(session, params["requestId"], intercept.done_page)
                    logger.info("[auth/intercept] captured the %s redirect", intercept.location_prefix)
                    return location
                await session.post("Fetch.continueRequest", {"requestId": params["requestId"]})
        finally:
            await session.close()
