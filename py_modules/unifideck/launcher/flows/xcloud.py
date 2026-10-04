"""xCloud CDP injection flow. NOT on the live launch path.

# unimported: nothing calls ``launch_xcloud``; the live path is
# ``services/launcher/browser_game.run_browser_game``. This module and the CDP
# cluster only it reaches (``launcher/cdp/xcloud_cdp``, ``cdp/xcloud_browser_shims``,
# ``launcher/cdp/steam_controller_popup*``) are dead; deleting them is audit
# register row 77.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Result
from unifideck.launcher.cdp.xcloud_cdp import run_cdp_inject
from unifideck.launcher.types.context import LaunchContext
from unifideck.launcher.types.errors import DependencyMissingError

from .browser_window import wait_for_browser_exit

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
logger = logging.getLogger(__name__)
_MAX_SESSION_SECONDS = 14400
_XCLOUD_CDP_PORT = 9223
_CDP_INJECT_TIMEOUT = 60.0
def _read_config_int(key: str, default: int) -> int:
    """Read config int."""
    from unifideck.utils.config_helpers import read_config_int_cold_start
    return read_config_int_cold_start(key, default)

async def launch_xcloud(
    ctx: LaunchContext,
    edge_browser: EdgeBrowser,
) -> Result:

    """Launch xcloud."""
    target_url = str(ctx.browser_url or "")
    logger.info(
        "[launcher.xcloud] launching: %s", target_url[:80],
    )
    if not edge_browser.is_installed:
        raise DependencyMissingError(
            "Microsoft Edge flatpak required for xCloud "
            "streaming",
            context={
                "game_id": ctx.game_id,
                "url": target_url,
            },
        )
    started = edge_browser.launch_browser_game(target_url)
    if not started:
        return Result(
            success=False,
            error="edge_launch_failed",
            store=ctx.store,
        )
    inject_task: asyncio.Task[bool] = asyncio.create_task(
        run_cdp_inject(
            port=_XCLOUD_CDP_PORT,
            launch_url=target_url,
            timeout=_CDP_INJECT_TIMEOUT,
            steam_controller_appid=int(ctx.steam_app_id or 0),
        ),
        name=f"xcloud_cdp_inject:{ctx.game_key}",
    )
    try:
        await wait_for_browser_exit(
            edge_browser,
            _read_config_int("launcher.browser_game_max_seconds", _MAX_SESSION_SECONDS),
            log_tag="launcher.xcloud",
        )
    finally:
        if not inject_task.done():
            inject_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await inject_task
    logger.info(
        "[launcher.xcloud] session ended: %s", ctx.game_key,
    )
    return Result(success=True, store=ctx.store)
