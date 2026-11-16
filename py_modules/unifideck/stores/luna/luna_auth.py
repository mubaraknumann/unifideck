"""LunaAuth: drives the interactive Amazon sign-in for Luna and decides
whether the resulting browser session is usable.

This is the Luna analogue of ``microsoft_auth.py`` / the xCloud browser
sign-in — but Luna has **no OAuth**: no authorize/token endpoint, no
authorization-code exchange, no token file. "Signed in" means the
interactive Amazon sign-in in the streaming browser left non-expired Amazon
session cookies in the Luna browser profile (F2, Req 1.2).

Flow (design "LunaBrowserAuth / LunaAuth", Req 1.1/1.2/1.5/1.6/1.7/1.8):

1. **Ensure the profile dir.** Create the dedicated Luna profile dir via
   :class:`LunaProfile`. A non-writable profile dir surfaces a
   ``profile_permission`` error and the sign-in session is **not** opened
   (Req 1.9).
2. **Open the browser at the sign-in page with the spoofer attached.**
   :class:`LunaBrowser` spawns the kiosk at ``{luna_host}{signin_path}``;
   once CDP is up, :class:`LunaPlatformSpoofer` attaches browser-level
   before any Luna script runs so the sign-in page already presents the
   spoofed Windows identity (Req 1.1).
3. **Poll until cookies appear or the 300 s timeout fires.** Repeatedly ask
   :class:`LunaBrowserAuth` whether the sign-in is observed complete (the UI
   left the Amazon sign-in page **and** non-expired Amazon session cookies
   are present per :class:`LunaSessionMonitor`). If the browser window is
   closed before that (early close) or no cookies ever persist, or the
   300 s budget elapses, the attempt failed.
4. **Report connected / not-connected and discard partial state.** On
   success the session is usable and ``connected`` is reported. On early
   close, no cookies, or timeout the kiosk is killed, any partial session
   state in the profile is discarded (the cookies cleared), and
   ``not_connected`` is reported with the distinguishing reason (Req 1.5,
   1.6, 1.8).

Testability: the browser spawn, CDP client construction, spoofer attach,
sign-in observation, and the clock/sleep are all injected, so
:meth:`LunaAuth.run_sign_in` can be driven to each terminal outcome
(connected / early-close / no-cookies / timeout) without a real browser.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
from unifideck.core.types import AuthResult
from unifideck.stores.luna.luna_browser import LunaBrowser
from unifideck.stores.luna.luna_browser_auth import LunaBrowserAuth
from unifideck.stores.luna.luna_platform_spoofer import (
    LunaPlatformSpoofer,
    LunaSpoofConfigError,
)
from unifideck.stores.luna.luna_profile import (
    LunaProfile,
    LunaProfileError,
)
from unifideck.stores.luna.luna_session_monitor import LunaSessionMonitor

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.luna.luna_config import LunaConfig

logger = logging.getLogger(__name__)

#: The store id used on every :class:`AuthResult` this module returns.
LUNA_STORE = "luna"

#: Interactive sign-in budget (Req 1.8). The user has this long from the
#: moment the sign-in page is opened to complete the Amazon sign-in before
#: the attempt is abandoned.
SIGN_IN_TIMEOUT_S = 300.0

#: How long to wait for the kiosk's CDP port to come up before giving up on
#: attaching the spoofer. The browser-game kiosk opens the debugging port a
#: moment after spawn.
_CDP_READY_TIMEOUT_S = 20.0

#: Poll cadences: how often to re-probe CDP readiness while waiting for the
#: port, and how often to re-check the sign-in state while the user signs in.
_CDP_POLL_INTERVAL_S = 0.5
_SIGN_IN_POLL_INTERVAL_S = 1.0

#: ``AuthResult.error`` reasons for the not-connected outcomes, kept as
#: constants so the store, the frontend mapping and tests agree on them.
ERR_PROFILE_PERMISSION = "profile_permission"
ERR_PROFILE_NOT_PERSISTED = "profile_not_persisted"
ERR_NO_BROWSER = "no_supported_browser"
ERR_SPOOFING_FAILURE = "platform_spoofing_failure"
ERR_SIGN_IN_CLOSED = "sign_in_closed"
ERR_SIGN_IN_NO_COOKIES = "sign_in_no_session"
ERR_SIGN_IN_TIMEOUT = "sign_in_timeout"


@dataclass(frozen=True)
class SignInOutcome:
    """The terminal result of the sign-in poll loop.

    ``status`` is the verdict; ``error`` is the not-connected reason (one of
    the ``ERR_*`` constants) or ``None`` when connected. The outcomes map
    1:1 to Req 1: ``connected`` (cookies persisted), ``closed`` (early
    close, Req 1.5), ``no_cookies`` (sign-in finished but nothing persisted,
    Req 1.6), ``timeout`` (300 s elapsed, Req 1.8).
    """

    status: str  # "connected" | "closed" | "no_cookies" | "timeout"
    error: str | None


class LunaAuth:
    """Drive the interactive Luna sign-in and report connected/not-connected.

    Owns the sign-in orchestration only; it composes :class:`LunaBrowser`
    (spawn), :class:`LunaBrowserAuth` (CDP observation),
    :class:`LunaSessionMonitor` (cookie-store truth),
    :class:`LunaPlatformSpoofer` (the sign-in page must already be spoofed),
    and :class:`LunaProfile` (profile dir). No OAuth,
    no token exchange.

    All collaborators default to real implementations but are injectable so
    the whole flow is drivable in tests without a browser.
    """

    def __init__(
        self,
        bus: EventBus | None,
        browser: LunaBrowser | None,
        session: LunaSessionMonitor,
        config: LunaConfig,
        *,
        browser_auth: LunaBrowserAuth | None = None,
        profile: LunaProfile | None = None,
        spoofer_factory: Callable[[LunaConfig], LunaPlatformSpoofer] | None = None,
        cdp_factory: Callable[[int], EdgeCDPClient] | None = None,
        sleep: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """Compose the sign-in flow over its collaborators."""
        self._bus = bus
        self._browser = browser if browser is not None else LunaBrowser(config)
        self._session = session
        self._config = config
        self._browser_auth = browser_auth or LunaBrowserAuth(session, config)
        self._profile = profile or LunaProfile.from_config(config.profile_dir)
        self._spoofer_factory = spoofer_factory or LunaPlatformSpoofer
        self._cdp_factory = cdp_factory or EdgeCDPClient
        if sleep is None:
            import asyncio

            self._sleep: Callable[[float], Any] = asyncio.sleep
        else:
            self._sleep = sleep
        if monotonic is None:
            import time

            self._monotonic: Callable[[], float] = time.monotonic
        else:
            self._monotonic = monotonic

    # ── Public entry point ───────────────────────────────────────────

    async def start_auth(self) -> AuthResult:
        """Open the interactive Luna sign-in and report the outcome (Req 1).

        Ensures the profile dir, opens the browser at the
        sign-in page with the spoofer attached, polls until cookies appear
        or the 300 s timeout fires, and returns a ``connected`` or
        ``not_connected`` :class:`AuthResult`. Discards partial session
        state on early close / no cookies / timeout.
        """
        # 1. Profile dir. A non-writable dir aborts before any browser opens
        #    (Req 1.9).
        try:
            self._profile.ensure_profile_dir()
        except LunaProfileError as exc:
            logger.warning("[LunaAuth] profile unusable: %s", exc)
            return self._not_connected(ERR_PROFILE_PERMISSION, url=exc.path)

        # 2. Spawn the kiosk at the sign-in page.
        launched = await self._launch(self._signin_url())
        if not launched:
            logger.warning("[LunaAuth] no supported browser found")
            return self._not_connected(ERR_NO_BROWSER)

        try:
            # 3. Bring up the spoofed sign-in session: verify the profile
            #    persisted, wait for CDP, and attach the spoofer. Returns a
            #    not-connected result instead of a CDP client on any failure.
            prepared = await self._prepare_signin_session()
            if isinstance(prepared, AuthResult):
                return prepared

            # 4. Poll the sign-in to a terminal outcome.
            outcome = await self.run_sign_in(prepared)
        finally:
            # Always tear the kiosk down — on success it has served its
            # purpose, on failure it must not linger holding the profile.
            await self._kill_browser()

        if outcome.status == "connected":
            logger.info("[LunaAuth] Luna sign-in connected")
            return AuthResult(success=True, store=LUNA_STORE, tokens_cached=True)

        # Not connected: discard any partial session state (Req 1.5/1.6/1.8).
        self._discard_partial_state()
        return self._not_connected(outcome.error or ERR_SIGN_IN_NO_COOKIES)

    async def _prepare_signin_session(self) -> EdgeCDPClient | AuthResult:
        """Verify the profile persisted, bring up CDP, and attach the spoofer.

        Returns the ready :class:`EdgeCDPClient` on success, or a
        not-connected :class:`AuthResult` on any failure (profile not
        persisted, CDP never came up, or a spoofing-config error). Extracted
        from :meth:`start_auth` so that orchestrator stays under the fan-out
        cap; behaviour is identical to the inline block it replaced.
        """
        # An absent profile dir here means the sign-in would not persist
        # (Req 8.4).
        try:
            self._profile.verify_persisted()
        except LunaProfileError as exc:
            logger.warning("[LunaAuth] profile not persisted: %s", exc)
            return self._not_connected(ERR_PROFILE_NOT_PERSISTED, url=exc.path)

        # Attach the spoofer so the sign-in page is already spoofed (Req 1.1).
        # A spoofing-config failure aborts the sign-in.
        cdp = self._cdp_factory(self._browser.cdp_port())
        if not await self._wait_for_cdp(cdp):
            logger.warning("[LunaAuth] CDP never came up; aborting sign-in")
            return self._not_connected(ERR_SPOOFING_FAILURE)
        try:
            await self._spoofer_factory(self._config).attach(cdp)
        except LunaSpoofConfigError as exc:
            logger.warning(
                "[LunaAuth] spoofer attach failed (reported=%r): %s",
                exc.reported, exc,
            )
            return self._not_connected(ERR_SPOOFING_FAILURE)
        return cdp

    # ── Poll loop (injectable clock/sleep; browser-free in tests) ─────

    async def run_sign_in(self, cdp: EdgeCDPClient) -> SignInOutcome:
        """Poll the sign-in until a terminal outcome (Req 1.5/1.6/1.8).

        Re-checks every :data:`_SIGN_IN_POLL_INTERVAL_S` seconds whether:

        * the kiosk window was closed before the sign-in finished → early
          close (Req 1.5);
        * the sign-in is observed complete and Amazon session cookies
          persisted → ``connected`` (Req 1.4);

        until the :data:`SIGN_IN_TIMEOUT_S` budget elapses → timeout
        (Req 1.8). A loop that times out while the UI had completed sign-in
        but no cookies persisted is reported as ``no_cookies`` (Req 1.6) so
        the frontend can tell "never finished" from "finished but did not
        persist a session".

        Driven against an injected ``cdp`` and the injected clock/sleep, so
        tests reach every branch without a browser.
        """
        deadline = self._monotonic() + SIGN_IN_TIMEOUT_S
        saw_signin_complete = False
        while True:
            if not self._browser_running():
                # Window gone. If cookies happen to be present it still
                # counts as connected (the user finished then closed it);
                # otherwise it is an early close.
                if self._session.is_usable():
                    return SignInOutcome(status="connected", error=None)
                return SignInOutcome(status="closed", error=ERR_SIGN_IN_CLOSED)

            observed = self._browser_auth.observe_signed_in(cdp)
            if observed.signed_in:
                return SignInOutcome(status="connected", error=None)
            saw_signin_complete = saw_signin_complete or observed.cdp_left_signin

            if self._monotonic() >= deadline:
                if saw_signin_complete:
                    # UI reached Luna but no session cookies persisted.
                    return SignInOutcome(
                        status="no_cookies", error=ERR_SIGN_IN_NO_COOKIES,
                    )
                return SignInOutcome(status="timeout", error=ERR_SIGN_IN_TIMEOUT)

            await self._sleep(_SIGN_IN_POLL_INTERVAL_S)

    # ── Collaborator seams ───────────────────────────────────────────

    async def _wait_for_cdp(self, cdp: EdgeCDPClient) -> bool:
        """Wait for the kiosk's CDP port to answer, up to the ready budget."""
        deadline = self._monotonic() + _CDP_READY_TIMEOUT_S
        while True:
            if cdp.probe_cdp():
                return True
            if self._monotonic() >= deadline:
                return False
            await self._sleep(_CDP_POLL_INTERVAL_S)

    async def _launch(self, url: str) -> bool:
        """Spawn the kiosk at ``url`` off the event loop (blocking spawn)."""
        import asyncio

        return await asyncio.to_thread(self._browser.launch, url)

    def _browser_running(self) -> bool:
        """Whether the kiosk browser process is still alive.

        A closed window means the sign-in session ended (Req 1.5). Resolved
        through the composed ``EdgeBrowser`` process handle; a missing or
        exited process reads as not-running.
        """
        proc = getattr(self._browser.edge, "process", None)
        if proc is None:
            return False
        poll = getattr(proc, "poll", None)
        if poll is None:
            return True
        return poll() is None

    async def _kill_browser(self) -> None:
        """Terminate the kiosk browser (idempotent)."""
        import asyncio

        try:
            await asyncio.to_thread(self._browser.kill)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaAuth] browser kill error: %s", exc)

    def _discard_partial_state(self) -> None:
        """Discard any partial session state written during a failed attempt.

        Clears the Luna profile's Amazon cookies so a half-finished sign-in
        leaves no stale session behind (Req 1.5, 1.6, 1.8). Best-effort —
        a clear failure is logged but does not change the not-connected
        verdict already decided.
        """
        try:
            self._session.clear_cookies()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaAuth] discard partial state failed: %s", exc)

    # ── URL + result helpers ─────────────────────────────────────────

    def _signin_url(self) -> str:
        """Compose the Luna sign-in URL from ``luna_host`` + ``signin_path``.

        ``signin_path`` is joined to the host with exactly one slash so a
        leading-slash or bare path both compose cleanly.
        """
        host = (self._config.luna_host or "").rstrip("/")
        path = self._config.signin_path or ""
        if not path:
            return host
        return f"{host}/{path.lstrip('/')}"

    @staticmethod
    def _not_connected(error: str, *, url: str | None = None) -> AuthResult:
        """Build a not-connected :class:`AuthResult` carrying ``error``."""
        return AuthResult(
            success=False, store=LUNA_STORE, error=error, url=url,
            tokens_cached=False,
        )
