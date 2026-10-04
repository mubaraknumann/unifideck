"""Microsoft sign-in with xbox.com's own client: the OAuth device-code flow.

Why this client: Microsoft Store Collections, the only accurate list of the
games a user owns, answers per calling app. The login.live.com client
Unifideck used up to 0.7.5 gets an empty list; xbox.com's client gets the
user's real library. Measured on-device, the same token also runs Xbox Cloud
Gaming (the gssv XSTS and the xgpuweb login), so one sign-in covers both.

How it works (measured on-device, 2026-10-02):

1. POST ``devicecode`` returns a short ``user_code`` and a ``device_code``.
2. The auth window opens ``microsoft.com/link?otc=<user_code>``. Microsoft
   redirects to ``login.live.com/oauth20_remoteconnect.srf`` with the code
   already filled in, so the user only presses "Allow access" (and signs
   in, if the shared Edge profile has no Microsoft session yet).
3. We poll the token endpoint until Microsoft says yes, no, or the code
   expires (``expires_in``, ~15 minutes).
4. On success the window is closed through CDP, which shuts Edge down
   cleanly so the profile's cookies reach disk. Xbox Cloud Gaming streams on
   those cookies, so the sign-in must happen in this window, in the shared
   profile, and not on a phone.

``device_code`` is a credential until it expires: it never leaves this
module and is never logged. The ``user_code`` goes to the frontend so it
can be shown if the window is closed early.

Events keep the browser flow's contract: ``STORE_AUTH_STARTED`` /
``STORE_AUTH_COMPLETE`` carry ``{store}``, ``STORE_AUTH_FAILED`` carries
``{store, error}``. Only the ``error`` values are new.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from unifideck.auth.url_file import remove_url_file, write_url_file_atomically
from unifideck.core.types import AuthResult, Events, Result
from unifideck.security import audit_auth_flow

from .tokens.endpoint import HttpReply, post_form

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.event_bus.event_bus import EventBus

    from .microsoft_config import MicrosoftConfig
    from .tokens import MicrosoftTokenManager

logger = logging.getLogger(__name__)

#: The file the auth launcher opens (``launcher/flows/auth.py``).
MS_AUTH_URL_FILE = "~/.local/share/unifideck/ms_auth_url.txt"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEFAULT_VERIFICATION_URI = "https://www.microsoft.com/link"
_DEFAULT_EXPIRES_IN = 900
_DEFAULT_INTERVAL = 5
_SLOW_DOWN_STEP = 5
_MAX_INTERVAL = 30
#: Long enough for the user to see Microsoft's "All done!" page.
_CONFIRMATION_PAUSE_SECONDS = 2.0
#: The poll's clock; a module name so tests can move time without touching
#: the event loop's own ``time.monotonic``.
_monotonic = time.monotonic

#: XErr codes that mean this account cannot use Xbox at all.
_ACCOUNT_XERR: dict[int, str] = {
    2148916233: "no_xbox_profile",
    2148916235: "region_unavailable",
    2148916238: "child_account",
}

#: Token-endpoint answers while polling, by OAuth ``error``.
_POLL_ERRORS: dict[str, str] = {
    "authorization_pending": "pending",
    "slow_down": "slow_down",
    "expired_token": "device_code_expired",
    "authorization_declined": "access_denied",
    "access_denied": "access_denied",
    "invalid_client": "client_rejected",
    "unauthorized_client": "client_rejected",
}


@dataclass(frozen=True)
class DeviceCodeGrant:
    """What ``devicecode`` handed back."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

    @property
    def window_url(self) -> str:
        """The page the auth window opens, with the code pre-filled."""
        query = urllib.parse.urlencode({"otc": self.user_code})
        return f"{self.verification_uri}?{query}"


def parse_grant(reply: HttpReply) -> DeviceCodeGrant | None:
    """The grant from a ``devicecode`` answer, or None if there is none."""
    body = reply.body
    device_code, user_code = body.get("device_code"), body.get("user_code")
    if reply.status != 200 or not isinstance(device_code, str) or not isinstance(user_code, str):
        return None
    uri = body.get("verification_uri")
    return DeviceCodeGrant(
        device_code=device_code,
        user_code=user_code,
        verification_uri=uri if isinstance(uri, str) and uri else _DEFAULT_VERIFICATION_URI,
        expires_in=_positive_int(body.get("expires_in"), _DEFAULT_EXPIRES_IN),
        interval=_positive_int(body.get("interval"), _DEFAULT_INTERVAL),
    )


def request_failure(reply: HttpReply) -> str:
    """Why ``devicecode`` gave no grant, as a STORE_AUTH_FAILED ``error``."""
    if reply.status is None:
        return "network_unreachable"
    if reply.error in ("invalid_client", "unauthorized_client"):
        return "client_rejected"
    return "device_code_request_failed"


def poll_verdict(reply: HttpReply) -> str:
    """``success``, ``pending``, ``slow_down``, ``transient`` or a failure reason."""
    if reply.status is None or reply.status == 429 or reply.status >= 500:
        return "transient"
    if reply.status == 200:
        return "success" if isinstance(reply.body.get("access_token"), str) else "transient"
    if not reply.error:
        return "transient"  # not an OAuth answer: a proxy or captive portal
    return _POLL_ERRORS.get(reply.error, "device_code_failed")


def _positive_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and value > 0 else default


def _pending_result(grant: DeviceCodeGrant) -> AuthResult:
    """What ``start_auth`` returns while the user approves (never the device code)."""
    return AuthResult(
        success=True,
        store="microsoft",
        url=grant.window_url,
        metadata={
            "pending": True,
            "flow": "device_code",
            "user_code": grant.user_code,
            "verification_uri": grant.verification_uri,
            "expires_in": grant.expires_in,
        },
    )


class MicrosoftDeviceAuth:
    """Runs one device-code sign-in at a time."""

    def __init__(
        self,
        bus: EventBus,
        tokens: MicrosoftTokenManager,
        config: MicrosoftConfig,
        edge: Callable[[], EdgeBrowser | None],
    ) -> None:
        """Initialize the instance.

        ``edge`` is a getter, not the browser: the store's Edge service is
        injected after construction (``services/bootstrap/store_injector``),
        so a reference taken here would stay ``None`` forever.
        """
        self._bus = bus
        self._tokens = tokens
        self._config = config
        self._edge = edge
        self._task: asyncio.Task[None] | None = None

    @audit_auth_flow(store="microsoft", method="oauth_device_code")
    async def start_auth(self) -> AuthResult:
        """Ask for a code, point the auth window at it, start polling."""
        if not self._config.is_valid():
            return AuthResult(success=False, error="config_invalid", store="microsoft")
        await self._reset_window()
        await self._emit(Events.STORE_AUTH_STARTED)
        grant = await self._request_grant()
        if isinstance(grant, str):
            return await self._start_failed(grant)
        if not await self._begin_polling(grant):
            return await self._start_failed("url_write_failed")
        logger.info(
            "[MicrosoftDeviceAuth] sign-in started (code valid %ds)", grant.expires_in,
        )
        return _pending_result(grant)

    async def _reset_window(self) -> None:
        """End a previous sign-in, and close any auth window left open.

        A lingering auth window would swallow the new ``--app`` launch.
        """
        await self.cancel()
        edge = self._edge()
        if edge is not None:
            await edge.prepare_auth_launch()

    async def _request_grant(self) -> DeviceCodeGrant | str:
        """A fresh code from Microsoft, or why there is none."""
        reply = await asyncio.to_thread(post_form, self._config.devicecode_url, {
            "client_id": self._config.client_id,
            "scope": self._config.scope,
        })
        return parse_grant(reply) or request_failure(reply)

    async def _begin_polling(self, grant: DeviceCodeGrant) -> bool:
        """Point the auth window at the code page and start waiting."""
        if not await write_url_file_atomically(MS_AUTH_URL_FILE, grant.window_url):
            return False
        self._task = asyncio.create_task(self._poll(grant), name="microsoft-device-code")
        return True

    async def cancel(self) -> None:
        """Stop a running sign-in silently (no event), closing its window."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self._close_window()
        await remove_url_file(MS_AUTH_URL_FILE)

    async def logout(self) -> Result:
        """Logout."""
        await self.cancel()
        await self._tokens.clear()
        await self._emit(Events.STORE_LOGOUT)
        return Result(success=True)

    # ── the poll ─────────────────────────────────────────────────────

    async def _poll(self, grant: DeviceCodeGrant) -> None:
        try:
            body = await self._wait_for_approval(grant)
            if body is not None:
                await self._finish(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[MicrosoftDeviceAuth] sign-in crashed")
            await self._fail("device_code_failed")

    async def _wait_for_approval(self, grant: DeviceCodeGrant) -> dict[str, Any] | None:
        """The token response once the user approves; None after reporting a failure."""
        deadline = _monotonic() + grant.expires_in
        interval = grant.interval
        params = {
            "grant_type": _DEVICE_GRANT,
            "client_id": self._config.client_id,
            "device_code": grant.device_code,
        }
        while _monotonic() < deadline:
            await asyncio.sleep(interval)
            reply = await asyncio.to_thread(post_form, self._config.token_url, params)
            verdict = poll_verdict(reply)
            if verdict == "success":
                return reply.body
            if verdict == "slow_down":
                interval = min(interval + _SLOW_DOWN_STEP, _MAX_INTERVAL)
            elif verdict not in ("pending", "transient"):
                await self._fail(verdict)
                return None
        await self._fail("device_code_expired")
        return None

    async def _finish(self, body: dict[str, Any]) -> None:
        await self._tokens.accept_token_body(body)
        problem = await self._account_problem()
        if problem is not None:
            await self._tokens.clear()
            await self._fail(problem)
            return
        await asyncio.sleep(_CONFIRMATION_PAUSE_SECONDS)
        await self._close_window()
        await remove_url_file(MS_AUTH_URL_FILE)
        logger.info("[MicrosoftDeviceAuth] signed in")
        await self._emit(Events.STORE_AUTH_COMPLETE)

    async def _account_problem(self) -> str | None:
        """Why this account cannot use Xbox, if Xbox said so.

        A network failure here is not a problem with the account: the
        tokens are good and the next sync will mint the chain.
        """
        if await self._tokens.build_chain() is not None:
            return None
        xerr = self._tokens.last_xerr
        return _ACCOUNT_XERR.get(xerr) if xerr is not None else None

    # ── reporting ────────────────────────────────────────────────────

    async def _start_failed(self, reason: str) -> AuthResult:
        await self._fail(reason, close=False)
        return AuthResult(success=False, error=reason, store="microsoft")

    async def _fail(self, reason: str, *, close: bool = True) -> None:
        logger.warning("[MicrosoftDeviceAuth] sign-in failed: %s", reason)
        if close:
            await self._close_window()
        await remove_url_file(MS_AUTH_URL_FILE)
        await self._emit(Events.STORE_AUTH_FAILED, error=reason)

    async def _close_window(self) -> None:
        edge = self._edge()
        if edge is None:
            return
        try:
            await edge.close_auth_browser()
        except Exception as e:
            logger.warning("[MicrosoftDeviceAuth] could not close the sign-in window: %s", e)

    async def _emit(self, event: Events, **payload: Any) -> None:
        with contextlib.suppress(Exception):
            await self._bus.emit(event, store="microsoft", **payload)
