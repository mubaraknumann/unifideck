"""Embedded-browser OAuth flow for GOG.

GOG's OAuth flow requires the user to authenticate against the
GOG.com login page in a real browser. ``GOGBrowserAuth`` orchestrates
the embedded CDP browser:

* opens the GOG OAuth URL in the browser overlay;
* injects a small script to capture the redirect URL containing the
  authorization code;
* hands the code off to ``tokens/oauth.py`` for exchange
  against access/refresh tokens.

Failure modes (user cancels, network drops, CDP disconnect) are
reported back to the auth facade as ``AuthResult`` envelopes with
explicit error codes.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.types import AuthResult, Events, Result
from unifideck.event_bus.event_bus import EventBus
from unifideck.security import audit_auth_flow

from .config import GOG_AUTH_URL_FILE, GOGConfig
from .tokens import ExchangeOutcome, GOGTokenManager

logger = logging.getLogger(__name__)
_GOG_COOKIE_DOMAIN = "gog.com"

class GOGBrowserAuth:
    """Gogbrowser auth."""

    def __init__(
        self,
        bus: EventBus,
        orchestrator: AuthOrchestrator,
        tokens: GOGTokenManager,
        config: GOGConfig,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._orch = orchestrator
        self._tokens = tokens
        self._config = config

    @audit_auth_flow(store="gog", method="oauth_browser")
    async def start_auth(self) -> AuthResult:
        """Start auth."""
        if not self._config.is_valid():
            return AuthResult(
                success=False,
                error="config_invalid",
                store="gog",
            )
        return await self._orch.run_flow(
            get_url=self._build_auth_url,
            allowed_uris=list(
                self._config.allowed_redirect_uris,
            ),
            exchange_code=self._exchange_code,
            background=True,
            write_url_file=GOG_AUTH_URL_FILE,
        )

    async def _build_auth_url(self) -> str:
        """Build auth URL."""
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "layout": "client2",
        }
        query = urllib.parse.urlencode(params, safe="/: ")
        url = f"{self._config.auth_url}?{query}"
        logger.info(
            "[GOGBrowserAuth] built OAuth URL: %s?client_id=REDACTED"
            "&redirect_uri=%s&response_type=%s&layout=%s",
            self._config.auth_url,
            self._config.redirect_uri,
            "code",
            "client2",
        )
        return url

    async def _exchange_code(self, code: str) -> AuthResult:
        """Exchange code.

        Maps the token manager's three-state outcome to an
        ``AuthResult`` error code. ``token_exchange_network_error``
        (offline / flaky connection, retries exhausted) is
        distinguished from ``token_exchange_failed`` (a definitive
        auth failure: bad/consumed code, bad body, or save failure)
        so the frontend can show an actionable "no internet" message
        instead of a cryptic code.
        """
        outcome = await self._tokens.exchange_code(code)
        if outcome is ExchangeOutcome.OK:
            logger.info(
                "[GOGBrowserAuth] token exchange successful",
            )
            return AuthResult(success=True, store="gog")
        if outcome is ExchangeOutcome.NETWORK_FAILED:
            logger.warning(
                "[GOGBrowserAuth] token exchange failed: network unreachable",
            )
            return AuthResult(
                success=False,
                error="token_exchange_network_error",
                store="gog",
            )
        return AuthResult(
            success=False,
            error="token_exchange_failed",
            store="gog",
        )

    async def logout(self, browser_monitor: Any | None = None) -> Result:
        """Logout."""
        self._orch.cancel_background()
        await self._tokens.clear()
        if browser_monitor is not None:
            try:
                await browser_monitor.clear_cookies_for_domain(
                    _GOG_COOKIE_DOMAIN,
                )
                logger.info(
                    "[GOGBrowserAuth] cleared cookies for %s",
                    _GOG_COOKIE_DOMAIN,
                )
            except Exception as e:
                logger.warning(
                    "[GOGBrowserAuth] cookie clear failed: %s",
                    e,
                )
        await self._bus.emit(Events.STORE_LOGOUT, store="gog")
        return Result(success=True)
