"""Sign xbox.com in, in the stream window, from the saved Microsoft sign-in.

Xbox Cloud Gaming streams on xbox.com's own sign-in (MSAL in the Edge
profile's localStorage). The Microsoft sign-in Unifideck holds does not
reach it, so a user who signed in once in the QAM was asked again by the
stream. When the window has no xbox.com session and a sign-in issued to
xbox.com's client is saved (``stores/microsoft/xbox_com_grant``), this hands
that sign-in to xbox.com through the page's own device-code flow:

1. one CDP session on the stream page sets a TV user agent and defines
   ``globalThis.deviceCode`` before any page script runs. xbox.com's TV app
   asks that host provider for its device code and token, and only mounts
   ``/play/login/deviceCode`` for a supported TV browser ("smart-tv" + a
   partner marker; not webOS/Tizen/Kepler, whose native bridges hang boot);
2. the page calls its own ``loadExternalTokensAndSignIn`` with our answer;
3. once MSAL holds an account the session is closed, which ends the user
   agent override and the injected script (measured: both are per-session),
   and a fresh session sends the window back to the game.

Measured on a Steam Deck 2026-11-03: the stream then opens signed in, and
expired tokens renew through MSAL's refresh token on later launches.
Nothing here signs in anyone without a saved sign-in: without one the window
keeps xbox.com's own sign-in page, and the log says why.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

from unifideck.cdp.page_inject import list_page_targets
from unifideck.launcher.cdp.cdp_primitives import cdp_command

logger = logging.getLogger(__name__)
_LOG = "[xbox_com_signin]"

XBOX_HOST = "www.xbox.com"
TV_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; SMART-TV) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 "
    "38F12DB1-C5DB-4749-AB22-A5C9E9EF866C"
)
#: True/False once the window shows xbox.com; null before (the kiosk starts
#: on a blank page that is already "complete", whose storage cannot be read).
#: Null is "not known yet", never "signed out": treating it as signed out made
#: every launch hand the sign-in over again.
HAS_SESSION_JS = (
    "(() => { if (location.hostname !== 'www.xbox.com' || document.readyState === 'loading')"
    " return null; try { const k = JSON.parse(localStorage.getItem('msal.account.keys') || '[]');"
    " return Array.isArray(k) && k.length > 0; } catch (e) { return null; } })()"
)
_LOCALE = re.compile(r"^[a-z]{2,3}-[a-z]{2,4}$", re.IGNORECASE)
_PAGE_WAIT_S = 30.0
_SIGNIN_WAIT_S = 30.0


def provider_script(token_answer: dict[str, Any]) -> str:
    """The ``globalThis.deviceCode`` host provider, answering with ``token_answer``."""
    token_json = json.dumps(json.dumps(token_answer))
    device_code = json.dumps(json.dumps({
        "device_code": "unifideck", "user_code": "UNIFIDECK",
        "verification_uri": "https://www.microsoft.com/link",
        "expires_in": 900, "interval": 1, "message": "",
    }))
    return (
        "(() => { if (location.hostname !== '" + XBOX_HOST + "') return;"
        f" const TOKEN = {token_json}, CODE = {device_code};"
        " globalThis.deviceCode = {"
        " getDeviceCode(cv, corr, resolve) { resolve(CODE); },"
        " checkForToken(code, cv, corr, resolve) { resolve(TOKEN); } }; })();"
    )


def device_code_url(page_url: str) -> str:
    """xbox.com's device-code page in the locale the stream page is using."""
    first = urlparse(page_url).path.strip("/").split("/")[0]
    locale = f"/{first}" if _LOCALE.match(first) else ""
    return f"https://{XBOX_HOST}{locale}/play/login/deviceCode?redirectUrl={quote('/play', safe='')}"


async def xbox_page(port: int, timeout: float) -> dict[str, Any] | None:  # noqa: ASYNC109
    """The stream window's xbox.com page target, once it exists."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        with contextlib.suppress(Exception):
            for target in await list_page_targets(port, timeout=3.0):
                if urlparse(str(target.get("url", ""))).hostname == XBOX_HOST:
                    return dict(target)
        await asyncio.sleep(0.5)
    return None


class PageSession:
    """One CDP session on the page; its overrides end when it closes."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws, self._id = ws, 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        result = (await cdp_command(self._ws, self._id, method, params)).get("result")
        return result if isinstance(result, dict) else {}

    async def value(self, expression: str) -> Any:
        """Evaluate ``expression``; None while the page is between documents."""
        try:
            reply = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        except RuntimeError:
            return None
        return reply.get("result", {}).get("value")

    async def known(self, expression: str, timeout: float) -> bool | None:  # noqa: ASYNC109
        """The first True/False ``expression`` gives; None if it never knew."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            value = await self.value(expression)
            if isinstance(value, bool):
                return value
            await asyncio.sleep(0.5)
        return None

    async def wait_for(self, expression: str, timeout: float) -> bool:  # noqa: ASYNC109
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.value(expression) is True:
                return True
            await asyncio.sleep(1.0)
        return False


async def _hand_over(session: PageSession, token_answer: dict[str, Any], page_url: str) -> bool:
    """Run xbox.com's device-code sign-in with our answer; True once MSAL holds it."""
    await session.call("Network.enable")
    await session.call("Network.setUserAgentOverride", {"userAgent": TV_USER_AGENT})
    await session.call("Page.addScriptToEvaluateOnNewDocument", {"source": provider_script(token_answer)})
    await session.call("Page.navigate", {"url": device_code_url(page_url)})
    await asyncio.sleep(2.0)  # let the navigation start before polling the old document
    return await session.wait_for(HAS_SESSION_JS, _SIGNIN_WAIT_S)


async def ensure_xbox_com_session(port: int, launch_url: str, token_file: str) -> str:
    """Sign the stream window's xbox.com in if it is not. Returns the outcome.

    ``"signed_in"`` (already was), ``"handed_over"``, ``"no_saved_signin"`` or
    ``"failed"``. Never raises except ``CancelledError``.
    """
    target = await xbox_page(port, _PAGE_WAIT_S)
    if target is None:
        logger.warning("%s no xbox.com page on CDP port %d; sign-in not checked", _LOG, port)
        return "failed"
    try:
        async with aiohttp.ClientSession() as http, http.ws_connect(
            target["webSocketDebuggerUrl"], heartbeat=10,
        ) as ws:
            session = PageSession(ws)
            state = await session.known(HAS_SESSION_JS, _PAGE_WAIT_S)
            if state is True:
                return "signed_in"
            if state is None:
                logger.warning("%s xbox.com never loaded in %.0f s; sign-in not checked",
                               _LOG, _PAGE_WAIT_S)
                return "failed"
            from unifideck.stores.microsoft.xbox_com_grant import load_xbox_com_grant
            grant = await asyncio.to_thread(load_xbox_com_grant, token_file)
            if not grant.ok:
                logger.info("%s xbox.com is not signed in and was not handed a sign-in: %s. "
                            "The stream window shows xbox.com's own sign-in.", _LOG, grant.reason)
                return "no_saved_signin"
            handed = await _hand_over(session, grant.body, str(target.get("url", "")))
    except Exception as e:
        # Never break the launch over this. Covers 3.10's asyncio.TimeoutError,
        # which is not the builtin there; CancelledError is not an Exception.
        logger.warning("%s handing the sign-in to xbox.com failed (%s); the stream "
                       "window shows xbox.com's own sign-in", _LOG, type(e).__name__)
        return "failed"
    if not handed:
        logger.warning("%s xbox.com did not accept the saved sign-in within %.0f s; "
                       "the stream window shows xbox.com's own sign-in", _LOG, _SIGNIN_WAIT_S)
        return "failed"
    await _open_game(target, launch_url)
    logger.info("%s xbox.com signed in from the saved Microsoft sign-in", _LOG)
    return "handed_over"


async def _open_game(target: dict[str, Any], launch_url: str) -> None:
    """Send the window back to the game, from a fresh session (normal user agent)."""
    with contextlib.suppress(Exception):
        async with aiohttp.ClientSession() as http, http.ws_connect(target["webSocketDebuggerUrl"]) as ws:
            await cdp_command(ws, 1, "Page.navigate", {"url": launch_url})
