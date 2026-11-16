"""Apply unifideck's choices to the Luna kiosk page before Play.

Luna keeps two user choices in the browser only, in ``localStorage`` on the
Luna site, and reads them once per page load (verified on a Steam Deck and in
the site's ``initial_app`` bundle):

* ``luna_video_quality_setting``: LIMITED / STANDARD / BEST;
* ``locale_override``: the UI language (``de_DE`` style).

The kiosk has already loaded the detail page by the time CDP is up, so the
values are written and the page reloaded once when one of them changed. They
then persist in the Luna profile, so later launches with the same choices
need no reload. An empty value (quality "Auto") is left untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from unifideck.stores.luna.luna_cdp import attach_to_page_target
from unifideck.stores.luna.luna_locale import LUNA_LOCALE_STORAGE_KEY
from unifideck.stores.luna.luna_platform_spoofer import _AiohttpBrowserCdpSession

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_platform_spoofer import BrowserCdpSession

logger = logging.getLogger(__name__)

#: Luna's own localStorage key for the streaming quality.
LUNA_QUALITY_STORAGE_KEY = "luna_video_quality_setting"

#: How long to let the reloaded page settle before the play activator
#: starts looking for the Play button (it retries on its own afterwards).
_RELOAD_SETTLE_S = 1.5


class LunaPageSettingsError(Exception):
    """The page could not be reached to read or write the settings."""


def page_settings_for(config: LunaConfig) -> dict[str, str]:
    """The localStorage entries to enforce for ``config`` (empty = skip)."""
    wanted = {
        LUNA_QUALITY_STORAGE_KEY: config.video_quality,
        LUNA_LOCALE_STORAGE_KEY: config.locale,
    }
    return {key: value for key, value in wanted.items() if value}


def build_apply_expression(settings: dict[str, str]) -> str:
    """JS that stores ``settings`` and reports whether any value changed."""
    return (
        "(() => { const want = " + json.dumps(settings) + "; let changed = false;"
        " for (const [k, v] of Object.entries(want)) {"
        " if (localStorage.getItem(k) !== v) { localStorage.setItem(k, v); changed = true; } }"
        " return changed; })()"
    )


def _changed(result: dict[str, Any]) -> bool:
    """Whether the apply evaluate reported a changed value."""
    if result.get("exceptionDetails"):
        raise LunaPageSettingsError("could not write Luna's page settings")
    inner = result.get("result")
    return isinstance(inner, dict) and inner.get("value") is True


async def run_apply(
    session: BrowserCdpSession, session_id: str, settings: dict[str, str],
) -> bool:
    """Write ``settings`` on the attached page; reload it when one changed.

    Returns whether the page was reloaded. Testable core of
    :func:`apply_page_settings`.
    """
    result = await session.send(
        "Runtime.evaluate",
        {"expression": build_apply_expression(settings), "returnByValue": True},
        session_id=session_id,
    )
    if not _changed(result):
        return False
    await session.send("Page.reload", {}, session_id=session_id)
    await asyncio.sleep(_RELOAD_SETTLE_S)
    return True


async def apply_page_settings(cdp: EdgeCDPClient, settings: dict[str, str]) -> bool:
    """Make the kiosk's Luna page use ``settings``; nothing to do is a no-op.

    Returns whether the page was reloaded to pick up a changed value.
    Raises :class:`LunaPageSettingsError` when the page cannot be reached.
    """
    if not settings:
        return False
    ws_url = cdp.get_browser_ws_url()
    if not ws_url:
        raise LunaPageSettingsError("no browser websocket on the Luna CDP port")
    session = _AiohttpBrowserCdpSession(ws_url)
    try:
        session_id = await attach_to_page_target(
            session,
            raise_no_target=lambda: LunaPageSettingsError("no Luna page target"),
            raise_no_session=lambda: LunaPageSettingsError(
                "could not attach to the Luna page",
            ),
        )
        reloaded = await run_apply(session, session_id, settings)
    finally:
        await session.close()
    logger.info(
        "[Luna] page settings %s %s", settings,
        "applied (page reloaded)" if reloaded else "already set",
    )
    return reloaded
