"""Apply the user's Luna streaming quality to the kiosk before Play.

Luna keeps the quality only in the browser, not in the Amazon account:
``localStorage["luna_video_quality_setting"]`` on the Luna origin, read once
when the page's ``ClientSettingsState`` is constructed (verified in the
site's ``initial_app`` bundle). The kiosk has already loaded the detail page
by the time CDP is up, so a changed value is written and the page reloaded
once; the value then persists in the Luna profile, so later launches with
the same choice need no reload.

"Auto" (an empty choice) leaves whatever Luna stored untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from unifideck.stores.luna.luna_cdp import attach_to_page_target
from unifideck.stores.luna.luna_platform_spoofer import _AiohttpBrowserCdpSession

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_platform_spoofer import BrowserCdpSession

logger = logging.getLogger(__name__)

#: Luna's own localStorage key for the streaming quality.
LUNA_QUALITY_STORAGE_KEY = "luna_video_quality_setting"

#: How long to let the reloaded page settle before the play activator
#: starts looking for the Play button (it retries on its own afterwards).
_RELOAD_SETTLE_S = 1.5


class LunaVideoQualityError(Exception):
    """The page could not be reached to read or write the quality."""


def build_apply_expression(quality: str) -> str:
    """JS that stores ``quality`` and reports whether the value changed."""
    key = json.dumps(LUNA_QUALITY_STORAGE_KEY)
    value = json.dumps(quality)
    return (
        "(() => { const prev = localStorage.getItem(" + key + ");"
        " if (prev === " + value + ") return false;"
        " localStorage.setItem(" + key + ", " + value + "); return true; })()"
    )


def _changed(result: dict[str, Any]) -> bool:
    """Whether the apply evaluate reported a changed value."""
    if result.get("exceptionDetails"):
        raise LunaVideoQualityError("could not write Luna's quality setting")
    inner = result.get("result")
    return isinstance(inner, dict) and inner.get("value") is True


async def run_apply(
    session: BrowserCdpSession, session_id: str, quality: str,
) -> bool:
    """Write ``quality`` on the attached page; reload it when it changed.

    Returns whether the page was reloaded. Testable core of
    :func:`apply_video_quality`.
    """
    result = await session.send(
        "Runtime.evaluate",
        {"expression": build_apply_expression(quality), "returnByValue": True},
        session_id=session_id,
    )
    if not _changed(result):
        return False
    await session.send("Page.reload", {}, session_id=session_id)
    await asyncio.sleep(_RELOAD_SETTLE_S)
    return True


async def apply_video_quality(cdp: EdgeCDPClient, quality: str) -> bool:
    """Make the kiosk's Luna page use ``quality``; ``""`` (Auto) is a no-op.

    Returns whether the page was reloaded to pick up a changed value.
    Raises :class:`LunaVideoQualityError` when the page cannot be reached.
    """
    if not quality:
        return False
    ws_url = cdp.get_browser_ws_url()
    if not ws_url:
        raise LunaVideoQualityError("no browser websocket on the Luna CDP port")
    session = _AiohttpBrowserCdpSession(ws_url)
    try:
        session_id = await attach_to_page_target(
            session,
            raise_no_target=lambda: LunaVideoQualityError("no Luna page target"),
            raise_no_session=lambda: LunaVideoQualityError(
                "could not attach to the Luna page",
            ),
        )
        reloaded = await run_apply(session, session_id, quality)
    finally:
        await session.close()
    logger.info(
        "[Luna] streaming quality %s%s", quality,
        " applied (page reloaded)" if reloaded else " already set",
    )
    return reloaded
