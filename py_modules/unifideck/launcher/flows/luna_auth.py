"""launcher/flows/luna_auth.py — run the Luna interactive sign-in inside the
gamescope session.

Sibling of ``flows/auth.py`` (Wine-prefix OAuth) and ``flows/storefront.py``,
but Luna has **no Wine prefix and no browser OAuth**: its session is Amazon
cookies in a dedicated, spoofed browser-game kiosk profile. In Gaming Mode a
window from a process Steam did not launch has no gamescope session and never
renders, so the sign-in must run from the ``unifideck-launcher`` process that
Steam started via a temporary shortcut (``UNIFIDECK_LUNA_ACTION=auth``) — this
module — rather than being spawned from the plugin backend.

It reuses the existing :class:`LunaAuth` orchestration unchanged (profile dir
+ kiosk spawn, spoofer attach, cookie poll, partial-state
discard). ``LunaAuth.start_auth`` blocks until the sign-in reaches a terminal
outcome, which is exactly what holds this launcher process open while the
kiosk window is up: Steam ends the shortcut's gamescope session the moment
this process exits, which would tear the window down (same contract as
``flows/browser_window.py``).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.core.types.results import Result

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext

logger = logging.getLogger(__name__)

LUNA_STORE = "luna"


async def handle_luna_auth(ctx: LaunchContext) -> Result:
    """Drive the Luna sign-in in this gamescope-launched process.

    Builds :class:`LunaConfig` from the standalone launcher config, runs the
    existing :class:`LunaAuth` flow (which spawns the spoofed kiosk, polls for
    Amazon cookies and discards partial state on failure), and maps its
    :class:`AuthResult` to a launcher :class:`Result`. Blocks for the whole
    sign-in because this process must outlive the kiosk window.
    """
    from unifideck.launcher.bootstrap import _load_standalone_config
    from unifideck.stores.luna.luna_auth import LunaAuth
    from unifideck.stores.luna.luna_browser import LunaBrowser
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_session_monitor import LunaSessionMonitor

    config = LunaConfig.from_config_manager(_load_standalone_config())
    ok, invalid = config.is_valid()
    if not ok:
        logger.warning(
            "[launcher.luna_auth] Luna config invalid, missing: %s",
            ", ".join(invalid),
        )
        return Result(
            success=False, store=LUNA_STORE, error="luna_config_invalid",
        )

    logger.info("[launcher.luna_auth] starting Luna sign-in in gamescope session")
    session = LunaSessionMonitor(config)
    browser = LunaBrowser(config)
    # No EventBus in the launcher process — LunaAuth takes bus as optional.
    auth = LunaAuth(None, browser, session, config)
    result = await auth.start_auth()
    logger.info(
        "[launcher.luna_auth] Luna sign-in finished: success=%s error=%s",
        result.success, result.error,
    )
    return Result(
        success=bool(result.success),
        store=LUNA_STORE,
        error=None if result.success else (result.error or "sign_in_failed"),
    )
