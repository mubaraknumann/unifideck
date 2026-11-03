"""stores/microsoft/xodus/signin_chain.py — the xCloud sign-in, in the Xbox PC window.

Xbox PC and Xbox Cloud Gaming use two different Microsoft credentials, and
neither can produce the other (measured 2026-11-03):

* The Xodus credential runs Xbox PC. xCloud's ``/connect`` refuses it
  (``MsaVeto``); it wants a console-transfer ticket, and Microsoft issues that
  only against an OAuth refresh token, which the Xodus sign-in never produces.
* ``xodus-cli login`` takes no token, so the OAuth sign-in cannot drive Xbox PC
  either. The Edge profile keeps no Microsoft website session afterwards: the
  only cookie that survives the window is a tracking one.

So a Deck with Xbox PC on needs both sign-ins. Rather than ambush the user at
the first stream, this runs the OAuth one at the end of the Xbox PC sign-in, in
the window that is already open and before it closes, so Connect asks once and
nothing asks again.

It never emits store events: the Xbox PC sign-in owns the store's outcome, and a
failure here must not report the store as signed out when Xbox PC did sign in.
The reason goes to the log instead. Skipped entirely when a usable OAuth
sign-in is already stored, so a second Connect never asks twice.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from unifideck.auth.edge_browser.cdp_session import CdpError, CdpPageSession
from unifideck.stores.microsoft.microsoft_device_auth import (
    DeviceCodeGrant,
    parse_grant,
    poll_verdict,
    request_failure,
)
from unifideck.stores.microsoft.tokens.endpoint import post_form

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager

    #: Returns why Xbox refuses this account, or None when it is usable.
    AccountCheck = Callable[[], Awaitable[str | None]]

logger = logging.getLogger(__name__)
_LOG = "[XboxPcSignIn/xCloud]"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_SLOW_DOWN_STEP = 5
_MAX_INTERVAL = 30
#: How long the user gets to approve before the window is let go. Shorter than
#: the code's own life: the window is open and they are in front of it.
APPROVAL_WAIT_S = 300.0


async def report_chain_outcome(chain: Callable[[int], Awaitable[str]], port: int) -> None:
    """Run ``chain`` and log what it meant. Never raises except ``CancelledError``.

    Lives here, not in the sign-in, because the Xbox PC sign-in has already
    succeeded by the time this runs and must not be failed by anything here.
    """
    try:
        outcome = await chain(port)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("%s the sign-in crashed", _LOG)
        return
    if outcome in ("signed_in", "skipped"):
        logger.info("%s %s", _LOG, outcome)
        return
    logger.warning(
        "%s Xbox PC is signed in, but the xCloud sign-in did not finish (%s). Streaming "
        "will ask for a Microsoft sign-in at the first game; Connect again to do it here.",
        _LOG, outcome,
    )


async def _navigate(page: CdpPageSession, url: str) -> bool:
    """Point the open sign-in window at ``url``."""
    try:
        await page.call("Page.enable")
        await page.post("Page.navigate", {"url": url})
    except (CdpError, OSError) as e:
        logger.warning("%s could not reach the sign-in window: %s", _LOG, type(e).__name__)
        return False
    return True


async def _approved(config: MicrosoftConfig, grant: DeviceCodeGrant) -> dict[str, Any] | str:
    """Poll until Microsoft says yes, or a reason why not. Never emits."""
    params = {
        "grant_type": _DEVICE_GRANT,
        "client_id": config.client_id,
        "device_code": grant.device_code,
    }
    interval = grant.interval
    deadline = asyncio.get_running_loop().time() + min(APPROVAL_WAIT_S, grant.expires_in)
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(interval)
        reply = await asyncio.to_thread(post_form, config.token_url, params)
        verdict = poll_verdict(reply)
        if verdict == "success":
            return dict(reply.body)
        if verdict == "slow_down":
            interval = min(interval + _SLOW_DOWN_STEP, _MAX_INTERVAL)
        elif verdict not in ("pending", "transient"):
            return verdict
    return "device_code_expired"


async def _store(
    tokens: MicrosoftTokenManager, body: dict[str, Any], account_problem: AccountCheck,
) -> str:
    """Keep the tokens if Xbox accepts the account, else drop them.

    ``account_problem`` is the device-code flow's own judgement, so a child or
    region-blocked account is treated identically in both paths.
    """
    await tokens.accept_token_body(body)
    problem = await account_problem()
    if problem is None:
        return "signed_in"
    await tokens.clear()
    return problem


async def run_chained_signin(
    port: int,
    config: MicrosoftConfig,
    tokens: MicrosoftTokenManager,
    account_problem: AccountCheck,
    page_factory: Callable[[], CdpPageSession] = CdpPageSession,
) -> str:
    """Run the xCloud sign-in in the window on ``port``. Returns the outcome.

    ``skipped`` when one is already stored. Never raises except
    ``CancelledError``: the Xbox PC sign-in has already succeeded by here, and
    nothing in this step may undo it.
    """
    if tokens.has_refresh_token:
        return "skipped"
    if not config.is_valid():
        logger.warning("%s cannot run: the Microsoft sign-in is not configured", _LOG)
        return "config_invalid"
    page = page_factory()
    try:
        reply = await asyncio.to_thread(post_form, config.devicecode_url, {
            "client_id": config.client_id, "scope": config.scope,
        })
        grant = parse_grant(reply)
        if grant is None:
            return request_failure(reply)
        if not await page.attach(port) or not await _navigate(page, grant.window_url):
            return "window_not_opened"
        logger.info("%s asking for approval in the sign-in window (code valid %ds)",
                    _LOG, grant.expires_in)
        outcome = await _approved(config, grant)
        if not isinstance(outcome, dict):
            return outcome
        return await _store(tokens, outcome, account_problem)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("%s did not finish: %s", _LOG, type(e).__name__)
        return "chain_failed"
    finally:
        await page.close()
