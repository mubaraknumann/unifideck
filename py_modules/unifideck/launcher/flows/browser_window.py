"""Hold a Steam shortcut's launcher open while its Edge window is up.

Moved out of ``flows/auth.py`` when browser games became its third caller;
the xCloud path kept a near-copy of it (``flows/xcloud._wait_for_session_end``)
that differed only in its config key and log text, and now uses this one.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

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
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, proc.wait),
                timeout=max_seconds,
            )
        except TimeoutError:
            logger.warning(
                "[%s] browser reached %ds timeout", log_tag, max_seconds,
            )
        return
    elapsed = 0.0
    while elapsed < max_seconds:
        await asyncio.sleep(5.0)
        elapsed += 5.0
