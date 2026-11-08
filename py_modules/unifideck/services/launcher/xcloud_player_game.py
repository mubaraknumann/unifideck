"""Stream an xCloud game in Unifideck's own player instead of xbox.com.

``try_own_player`` returns None whenever the own player cannot run (turned
off, no saved OAuth sign-in, a title xCloud does not list, no browser), and the
caller falls back to the xbox.com stream. Every such reason is logged with what
was missing. Otherwise it blocks until the window closes and always ends the
cloud session, a Stop or a Steam "Exit game" included.

Turn it off with ``launcher.xcloud_player: "xbox_com"`` in the plugin config.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Result

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext

    from .service import LauncherService

logger = logging.getLogger(__name__)
_LOG = "[LauncherService/xcloud_player]"
#: A play session's ceiling before the launcher stops waiting (4 h).
_MAX_SESSION_SECONDS = 14400


def _config(svc: LauncherService, key: str) -> Any:
    with contextlib.suppress(Exception):
        return svc._config.get(key) if svc._config else None
    return None


def token_file(svc: LauncherService) -> str:
    """``microsoft.token_file`` from the launcher's config, or the default."""
    from unifideck.stores.microsoft.xbox_com_grant import DEFAULT_TOKEN_FILE
    value = _config(svc, "stores.microsoft.token_file")
    return value if isinstance(value, str) and value else DEFAULT_TOKEN_FILE


def own_player_enabled(svc: LauncherService) -> bool:
    """False only when the config asks for the xbox.com stream."""
    return bool(_config(svc, "launcher.xcloud_player") != "xbox_com")


async def _wait(proc: subprocess.Popen[bytes]) -> None:
    from unifideck.launcher.flows.browser_window import tag_browser_window
    from unifideck.utils.config_helpers import read_config_int_cold_start
    limit = read_config_int_cold_start("launcher.browser_game_max_seconds", _MAX_SESSION_SECONDS)
    tagger = tag_browser_window(getattr(proc, "args", None), log_tag="xcloud_player")
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, proc.wait),
                                   limit)
    finally:
        if tagger is not None:
            tagger.set()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _end(proc: subprocess.Popen[bytes] | None) -> None:
    """Close the window: signal its whole process group, then wait.

    Not ``proc.terminate()``: a browser is a tree of processes, and a launcher
    in front of it (``flatpak run`` before 0.8.0) does not forward a signal, so
    the browser kept running (measured). The window was started with
    ``start_new_session``, so its group id is the pid we hold, and signalling the
    group reaches every process even after the first one has exited.
    """
    if proc is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        for _ in range(50):
            # Reap the leader first. It is our child, so once it exits it
            # stays a zombie in the group until waited on, and the group
            # probe counts a zombie as alive: every close waited out both
            # 5 s windows and sent SIGKILL to a window that had already shut.
            proc.poll()
            if not _group_alive(proc.pid):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
                return
            time.sleep(0.1)


class _InvalidCountryError(Exception):
    """Cloud Gaming refuses this location. xbox.com would refuse it too."""


async def _prepare(svc: LauncherService, url: str) -> tuple[Any, ...] | None:
    """``(login, api, title_id, browser)`` or None after logging why not.

    Raises :class:`_InvalidCountryError` when no stream can start at all.
    """
    from unifideck.launcher.xcloud_player import browser, credentials
    from unifideck.launcher.xcloud_player.api import XcloudApi

    login = await asyncio.to_thread(credentials.stream_login, token_file(svc))
    if login.invalid_country:
        raise _InvalidCountryError(login.reason)
    if not login.ok:
        logger.info("%s not used: %s. Streaming through xbox.com instead.", _LOG, login.reason)
        return None
    api = XcloudApi(login.base_uri, login.gs_token)
    product_id = url.rstrip("/").rsplit("/", 1)[-1]
    title_id = await asyncio.to_thread(api.title_id, product_id)
    if not title_id:
        logger.info("%s not used: xCloud does not list product %s. Streaming through "
                    "xbox.com instead.", _LOG, product_id)
        return None
    picked = await asyncio.to_thread(browser.browser_cmd, svc._edge_browser.find_cmd())
    if picked is None:
        logger.warning("%s not used: the plugin's browser is not installed yet", _LOG)
        return None
    return login, api, title_id, picked


async def try_own_player(svc: LauncherService, ctx: LaunchContext, url: str) -> Result | None:
    """Play the stream in the own player; None to fall back to xbox.com."""
    if not own_player_enabled(svc):
        return None
    try:
        prepared = await _prepare(svc, url)
    except _InvalidCountryError:
        from unifideck.launcher.rpc import emit_stage
        logger.warning("%s %s not started: Xbox Cloud Gaming refuses this connection's "
                       "location (InvalidCountry)", _LOG, ctx.game_key)
        await emit_stage(svc._bus, i18n_title_key="toasts.header.xboxCloud",
                         i18n_key="toasts.launcher.invalidCountry", game_title=ctx.game_key,
                         priority="normal", severity="warning")
        return Result(success=False, error="xcloud_invalid_country", store=ctx.store)
    if prepared is None:
        return None
    return await _run_player(svc, ctx, prepared)


async def _run_player(
    svc: LauncherService, ctx: LaunchContext, prepared: tuple[Any, ...],
) -> Result:
    from unifideck.launcher.xcloud_player import browser, credentials
    from unifideck.launcher.xcloud_player.server import PlayerServer

    login, api, title_id, (cmd, name) = prepared
    server = PlayerServer(api, title_id, lambda: credentials.transfer_ticket(login.refresh_token))
    proc: subprocess.Popen[bytes] | None = None
    try:
        page = await server.start()
        proc = await asyncio.to_thread(browser.launch, cmd, page)
        svc._active_subprocess = proc
        logger.info("%s streaming %s in %s", _LOG, title_id, name)
        await _wait(proc)
        logger.info("%s %s ended (window closed)", _LOG, ctx.game_key)
        return Result(success=True, store=ctx.store)
    finally:
        _end(proc)
        await server.stop()
