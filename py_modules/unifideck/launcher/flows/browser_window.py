"""Hold a Steam shortcut's launcher open while its Edge window is up.

Moved out of ``flows/auth.py`` when browser games became its third caller;
the xCloud path kept a near-copy of it (``flows/xcloud._wait_for_session_end``)
that differed only in its config key and log text, and now uses this one.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser

logger = logging.getLogger(__name__)


async def wait_for_browser_exit(
    edge_browser: EdgeBrowser,
    max_seconds: int,
    *,
    log_tag: str,
) -> None:
    """Block until the Edge window closes, or ``max_seconds`` elapses.

    The block is the point: the launcher process must outlive the
    window, because Steam ends the shortcut's gamescope session the
    moment the process exits and that tears the window down with it.

    Shared by every flow that opens an Edge window from a Steam shortcut:
    sign-in, the storefront (a much longer ceiling: a user browses a shop
    for far longer than they sign in) and browser games (longest: a play
    session). ``log_tag`` names the calling flow so the timeout warning
    is attributable.

    Falls back to a polling loop when there is no process handle:
    ``launch_*`` returned True but ``self.process`` was cleared, e.g.
    a crash detected in between.
    """
    proc = edge_browser.process
    if proc is not None:
        tagger = tag_browser_window(getattr(proc, "args", None), log_tag=log_tag)
        try:
            await _wait_or_close(edge_browser, proc, max_seconds, log_tag)
        finally:
            if tagger is not None:
                tagger.set()
        return
    elapsed = 0.0
    while elapsed < max_seconds:
        await asyncio.sleep(5.0)
        elapsed += 5.0


def _class_of(args: Any) -> str | None:
    """The ``--class=`` a browser was started with (its X ``WM_CLASS``)."""
    if not isinstance(args, (list, tuple)):
        return None
    return next((str(a).split("=", 1)[1] for a in args
                 if str(a).startswith("--class=")), None)


def tag_browser_window(args: Any, *, log_tag: str) -> threading.Event | None:
    """In Gaming Mode, mark the browser's window as the launched app's.

    Gamescope brings a window forward when it knows which Steam app owns it.
    It reads that from the window's ``STEAM_GAME`` property, or else walks
    the window's process ancestry up to Steam's ``reaper AppId=``. When
    Steam starts the shortcut through a compatibility tool, the launcher
    escapes the container and the browser no longer descends from the reaper
    (Steam client 2026-11 applies the global Steam Play tool to Unifideck's
    temporary shortcuts), so the window stays hidden behind Gaming Mode. The
    window tagger games use sets the property; this points it at the
    browser's window class. Returns the tagger's stop event, or None.
    """
    from unifideck.auth.edge_browser.env import clean_env
    from unifideck.launcher.proton.infrastructure.gamescope_window_tagger import (
        start_window_tagger,
    )

    wm_class = _class_of(args)
    if not wm_class:
        return None
    env = clean_env()
    if not env.get("GAMESCOPE_WAYLAND_DISPLAY"):
        return None
    try:
        appid = int(env.get("SteamAppId") or env.get("SteamGameId") or 0)
    except ValueError:
        appid = 0
    # For a non-Steam shortcut ``SteamGameId`` is Steam's 64-bit game id,
    # ``(appid << 32) | 0x02000000``; ``STEAM_GAME`` takes the 32-bit app id.
    if appid > 0xFFFFFFFF:
        appid >>= 32
    if appid <= 0:
        logger.warning(
            "[%s] the browser window may stay hidden behind Gaming Mode: no "
            "Steam app id in the launcher's environment to tag it with",
            log_tag,
        )
        return None
    return start_window_tagger(appid, wm_class)


async def _wait_or_close(
    edge_browser: EdgeBrowser, proc: Any, max_seconds: int, log_tag: str,
) -> None:
    """Wait for the browser to exit; close it on timeout or cancellation."""
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, proc.wait),
            timeout=max_seconds,
        )
    except TimeoutError:
        logger.warning(
            "[%s] browser reached %ds timeout; closing it", log_tag, max_seconds,
        )
        edge_browser.kill()
    except asyncio.CancelledError:
        # Steam's Stop: the browser must go with the launcher. It runs in
        # its own session, so nothing else ends it once Steam launches the
        # shortcut through the container escape (the escaped launcher is
        # outside Steam's reaper). Left running, it hides behind Gaming
        # Mode and holds the shared profile, so every later sign-in hands
        # off to it and every store window is refused.
        logger.info("[%s] launch cancelled; closing the browser", log_tag)
        edge_browser.kill()
        raise
