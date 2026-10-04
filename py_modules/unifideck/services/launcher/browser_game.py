"""Launch a browser game: an Edge kiosk window on its URL, held open while it runs.

Browser games are xCloud streams and itch.io HTML5 games
(``launcher/browser_games``). This was ``LauncherService._launch_xcloud``,
and every line of it was already generic except the xbox.com URL, which now
arrives on ``LaunchContext.browser_url``. Moved out of ``service.py`` when it
became shared, which also keeps that file under its size cap.

Deliberately not the Proton/native orchestrator: no cloud-save sync, no
prefix and no window tagger. The Edge window inherits Steam's game identity
from the launcher process that ``RunGame`` started, which is what brings it
to the foreground in Gaming Mode (``docs/gaming-mode-foreground.md``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Result
from unifideck.launcher.flows.browser_window import wait_for_browser_exit
from unifideck.launcher.rpc import emit_stage

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext

    from .service import LauncherService

logger = logging.getLogger(__name__)

#: A play session's ceiling before the launcher stops waiting (4 h).
_MAX_SESSION_SECONDS = 14400


def _read_config_int(key: str, default: int) -> int:
    from unifideck.utils.config_helpers import read_config_int_cold_start
    return read_config_int_cold_start(key, default)


async def _edge_missing(svc: LauncherService, ctx: LaunchContext) -> Result | None:
    """Abort result when Edge isn't installed, else ``None`` to continue.

    Checked before ``GAME_LAUNCHED`` so a no-op does not emit a launch/stop
    pair.
    """
    if svc._edge_browser.is_installed:
        return None
    logger.warning("[LauncherService] browser game aborted: Edge not installed")
    await emit_stage(
        svc._bus, i18n_key="toasts.launcher.browserRequired",
        game_title=ctx.game_key, severity="error", priority="normal",
    )
    return Result(success=False, error="edge_not_installed", store=ctx.store)


async def run_browser_game(svc: LauncherService, ctx: LaunchContext) -> Result:
    """Open ``ctx.browser_url`` in an Edge kiosk and block until it closes."""
    from unifideck.core.types.events import Events

    abort = await _edge_missing(svc, ctx)
    if abort is not None:
        return abort
    url = str(ctx.browser_url)
    await svc._bus.emit(
        Events.GAME_LAUNCHED, store=ctx.store, game_id=ctx.game_id,
        title="", app_id=0,  # LaunchContext carries neither
    )
    if ctx.browser_kind == "stream":
        # A cloud stream signs in to its service before the game starts.
        await emit_stage(svc._bus, i18n_key="toasts.launcher.signingIn",
                         game_title=ctx.game_key)
    try:
        # ``launch_browser_game`` is synchronous: ``Popen`` blocks for about
        # half a second while Edge initialises.
        launched = await asyncio.to_thread(svc._edge_browser.launch_browser_game, url)
        if not launched:
            return Result(success=False, error="edge_launch_failed", store=ctx.store)
        # Block like the native and Windows paths ``await proc.wait()``.
        # Returning at once made GAME_STOPPED fire immediately: no playtime,
        # a stale running indicator, and Stop did nothing. Registering Edge
        # as the active subprocess is what lets Stop's SIGTERM reach it.
        svc._active_subprocess = svc._edge_browser.process
        await wait_for_browser_exit(
            svc._edge_browser,
            _read_config_int("launcher.browser_game_max_seconds", _MAX_SESSION_SECONDS),
            log_tag="launcher.browser_game",
        )
        logger.info("[LauncherService] browser game %s ended (window closed)", ctx.game_key)
        return Result(success=True, store=ctx.store)
    except Exception as e:
        logger.exception("[LauncherService] browser game launch failed")
        return Result(success=False, error=str(e))
    finally:
        svc._active_subprocess = None
        logger.info("[LauncherService] browser game %s stopped", ctx.game_key)
        await svc._bus.emit(Events.GAME_STOPPED, store=ctx.store, game_id=ctx.game_id)
