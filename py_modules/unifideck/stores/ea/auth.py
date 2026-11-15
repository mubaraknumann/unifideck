"""EA sign-in: EA's own login page in the sign-in window, tokens kept by ``maxima-cli``.

``maxima-cli auth-url`` builds the URL (``JUNO_PC_CLIENT`` OAuth with PKCE and
EA's ``pc_sign``), the window opens it, and EA ends the login with a 302 to
``qrc:/html/login_successful.html?code=…``. Chromium never follows that scheme,
so the code is read off the 302 itself (``auth/redirect_intercept.py``).
``maxima-cli auth-exchange`` then trades it for tokens and stores them; the
plugin never holds an EA token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.auth.browser_types import RedirectIntercept
from unifideck.core.types import AuthResult, Events, Result
from unifideck.security import audit_auth_flow

from .cli import MaximaCli, MaximaCliError
from .ids import STORE

if TYPE_CHECKING:
    from unifideck.auth.orchestrator import AuthOrchestrator
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

AUTH_URL_FILE = "~/.local/share/unifideck/ea_auth_url.txt"

EA_REDIRECT = RedirectIntercept(
    # The whole host: only a Location to qrc: is acted on, and the last hop's path may vary.
    url_pattern="https://accounts.ea.com/*",
    location_prefix="qrc:",
)


class EAAuthFlow:
    """Sign-in window → captured code → ``maxima-cli auth-exchange``."""

    def __init__(self, bus: EventBus, orchestrator: AuthOrchestrator, cli: MaximaCli) -> None:
        self._bus = bus
        self._orch = orchestrator
        self._cli = cli

    @audit_auth_flow(store=STORE, method="oauth_cli")
    async def start_auth(self) -> AuthResult:
        """Reuse a still-valid stored sign-in, else open the sign-in window."""
        if not self._cli.cli_path:
            return AuthResult(success=False, error="maxima_not_found", store=STORE)
        if await self.signed_in():
            logger.info("[EA] stored sign-in still valid; skipping sign-in")
            await self._bus.emit(Events.STORE_AUTH_COMPLETE, store=STORE)
            return AuthResult(success=True, store=STORE)
        return await self._orch.run_flow(
            get_url=self._cli.auth_url,
            allowed_uris=[],
            exchange_code=self._exchange,
            background=True,
            write_url_file=AUTH_URL_FILE,
            intercept=EA_REDIRECT,
        )

    async def signed_in(self) -> bool:
        """Whether ``maxima-cli`` holds a usable EA sign-in."""
        try:
            return await self._cli.signed_in()
        except MaximaCliError as e:
            logger.warning("[EA] could not check the stored sign-in: %s", e)
            return False

    async def logout(self) -> Result:
        """Delete the stored EA sign-in."""
        try:
            await self._cli.logout()
        except MaximaCliError as e:
            return Result(success=False, store=STORE, error=f"ea_logout_failed: {e.code}")
        return Result(success=True, store=STORE)

    async def _exchange(self, code: str) -> AuthResult:
        try:
            await self._cli.auth_exchange(code)
        except MaximaCliError as e:
            logger.warning("[EA] code exchange failed: %s", e)
            return AuthResult(success=False, error=f"ea_exchange_failed: {e.code}", store=STORE)
        logger.info("[EA] signed in")
        return AuthResult(success=True, store=STORE)
