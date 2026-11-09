"""Get an xCloud stream window from "launched" to "playing" without the user.

Two steps, run beside every stream (``services/launcher/browser_game``):

1. ``xbox_com_signin.ensure_xbox_com_session``: hand the saved Microsoft
   sign-in to xbox.com when it holds none.
2. ``press_start_gate``: xbox.com stops a ready stream behind a "ready to play"
   button, because the browser wants a user gesture before it plays. The game is
   already streaming underneath. A click sent over CDP counts as a trusted
   gesture, so it is pressed once here; the user already pressed Play in Steam.

The gate is matched on structure, not text (the label is localised): exactly
one visible ``PopupScreen`` button while the stream's video track is live.
xbox.com's error popups use the same component but come without a live stream,
so they are never pressed. Turning off xbox.com's "launch fullscreen" setting
does not remove the gate (measured 2026-11-03).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .xbox_com_signin import PageSession, ensure_xbox_com_session, xbox_page

logger = logging.getLogger(__name__)
_LOG = "[xcloud_stream]"

GATE_JS = (
    "(() => { const live = [...document.querySelectorAll('video')].some(v => v.srcObject"
    " && v.srcObject.getVideoTracks && v.srcObject.getVideoTracks().some(t => t.readyState"
    " === 'live')); if (!live) return null; const b = [...document.querySelectorAll("
    "'button[class*=\"PopupScreen-module__button\"]')].filter(x => x.offsetParent !== null);"
    " if (b.length !== 1) return null; const r = b[0].getBoundingClientRect();"
    " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"
)
#: How long a stream may take to reach its gate (queue + provisioning).
GATE_WAIT_S = 240.0
_PAGE_WAIT_S = 30.0


async def _click(session: PageSession, where: dict[str, Any]) -> None:
    for kind in ("mousePressed", "mouseReleased"):
        await session.call("Input.dispatchMouseEvent", {
            "type": kind, "x": where["x"], "y": where["y"], "button": "left", "clickCount": 1,
        })


async def press_start_gate(port: int, timeout: float = GATE_WAIT_S) -> str:  # noqa: ASYNC109
    """Press the "ready to play" button once it shows. Returns the outcome.

    ``pressed``, ``not_seen`` (no gate before ``timeout``: already started, or
    the user pressed it first) or ``failed``. Never raises except
    ``CancelledError``.
    """
    target = await xbox_page(port, _PAGE_WAIT_S)
    if target is None:
        return "failed"
    try:
        async with aiohttp.ClientSession() as http, http.ws_connect(
            target["webSocketDebuggerUrl"], heartbeat=10,
        ) as ws:
            session = PageSession(ws)
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                where = await session.value(GATE_JS)
                if isinstance(where, dict):
                    await _click(session, where)
                    return "pressed"
                await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning("%s could not press the start button (%s)", _LOG, type(e).__name__)
        return "failed"
    return "not_seen"


async def prepare_stream(port: int, launch_url: str, token_file: str) -> str:
    """Sign xbox.com in if needed, then start the stream. Returns the gate outcome."""
    signin = await ensure_xbox_com_session(port, launch_url, token_file)
    gate = await press_start_gate(port)
    logger.info("%s sign-in: %s; start button: %s", _LOG, signin, gate)
    return gate
