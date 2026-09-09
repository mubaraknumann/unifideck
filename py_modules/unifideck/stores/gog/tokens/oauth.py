"""OAuth protocol — exchange auth code for tokens, refresh expired tokens.

``_TokenOAuth`` speaks GOG's OAuth 2.0 endpoint :

* ``exchange_code(auth_code)`` — exchanges the authorization code
  obtained from ``auth.py`` for access/refresh tokens and
  reports the outcome as an :class:`ExchangeOutcome`;
* ``refresh(refresh_token)`` — POSTs the refresh token and returns a
  new pair of access/refresh tokens (GOG rotates refresh tokens, so
  the old refresh token becomes invalid after refresh).

HTTP calls go through ``http.py`` for the bundled CA chain.
The code-exchange path distinguishes a *transient* network failure
(``http.py`` raises :class:`TransientNetworkError`, and we retry a
few times) from a *definitive* auth failure (bad/consumed code, bad
body, or save failure — no retry, because a GOG authorization code
is single-use). ``exchange_code`` therefore returns a three-state
:class:`ExchangeOutcome` rather than a bare bool.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from enum import Enum
from typing import TYPE_CHECKING, Any

from unifideck.stores.gog.http import (
    TransientNetworkError,
    fetch_json_get,
)

from .user_info import GOGUserInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from unifideck.stores.gog.config import GOGConfig

    SaveCallback = Callable[[str, str], Awaitable[bool]]
logger = logging.getLogger(__name__)

# Bounded retry for the code exchange ONLY. The OAuth code is
# single-use, so we retry solely on TransientNetworkError (the
# request never reached GOG). Backoff sleeps run before attempts 2
# and 3 → ~3s worst case, well inside the orchestrator's 300s
# deadline and the frontend's 10-min ceiling.
_MAX_EXCHANGE_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 2.0)

class ExchangeOutcome(Enum):
    """Result of a GOG authorization-code exchange."""

    OK = "ok"
    AUTH_FAILED = "auth_failed"
    NETWORK_FAILED = "network_failed"

class _TokenOAuth:
    """Token oauth."""

    def __init__(self, *, config: GOGConfig, save_callback: SaveCallback) -> None:
        """Initialize the instance."""
        self._config = config
        self._save = save_callback

    async def exchange_code(self, auth_code: str) -> ExchangeOutcome:
        """Exchange code.

        Retries only on a transient network failure (the request never
        reached GOG); a definitive HTTP response or bad body is never
        retried because the authorization code is single-use.
        """
        params = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": self._config.redirect_uri,
        }
        return await self._exchange_with_retry(params)

    async def refresh_if_stale(
        self,
        *,
        access_token: str | None,
        refresh_token: str | None,
        age_seconds: float,
    ) -> bool:
        """Refresh if stale."""
        threshold = self._config.token_refresh_threshold_seconds
        if age_seconds < threshold and access_token:
            return True
        if not refresh_token:
            logger.info(
                "[GOGTokens] no refresh token — session is dead",
            )
            return False
        logger.info(
            "[GOGTokens] token age %.0fs ≥ %ds, refreshing",
            age_seconds,
            threshold,
        )
        params = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        return await self._token_request(params)

    async def fetch_user_info(
        self,
        access_token: str,
        fallback: GOGUserInfo,
    ) -> GOGUserInfo:
        """Fetch user info."""
        url = f"{self._config.base_url}/userData.json"
        data = await fetch_json_get(
            url,
            bearer=access_token,
            user_agent=self._config.user_agent,
            timeout=10.0,
            log_prefix="[GOGTokens] userData",
        )
        if not isinstance(data, dict):
            return fallback
        return GOGUserInfo(
            username=str(
                data.get("username", "") or fallback.username,
            ),
            galaxy_user_id=str(
                data.get("galaxyUserId", "") or fallback.galaxy_user_id,
            ),
        )

    async def _exchange_with_retry(
        self, params: dict[str, str],
    ) -> ExchangeOutcome:
        """Run the code exchange with a bounded, transient-only retry.

        A ``TransientNetworkError`` means the request never reached
        GOG, so retrying with the same (still-unconsumed) code is
        safe. Any definitive result (HTTP status / bad body / save
        failure) short-circuits with ``AUTH_FAILED`` — retrying a
        single-use code would only fail again.
        """
        url = f"{self._config.token_url}?{urllib.parse.urlencode(params)}"
        for attempt in range(1, _MAX_EXCHANGE_ATTEMPTS + 1):
            try:
                data = await fetch_json_get(
                    url,
                    user_agent=self._config.user_agent,
                    timeout=15.0,
                    log_prefix="[GOGTokens] token endpoint",
                    raise_on_transient=True,
                )
            except TransientNetworkError as e:
                if attempt >= _MAX_EXCHANGE_ATTEMPTS:
                    logger.warning(
                        "[GOGTokens] token exchange gave up after "
                        "%d network attempts: %s",
                        _MAX_EXCHANGE_ATTEMPTS,
                        e,
                    )
                    return ExchangeOutcome.NETWORK_FAILED
                backoff = _BACKOFF_SECONDS[attempt - 1]
                logger.info(
                    "[GOGTokens] token exchange network error "
                    "(attempt %d/%d), retrying in %.0fs: %s",
                    attempt,
                    _MAX_EXCHANGE_ATTEMPTS,
                    backoff,
                    e,
                )
                await asyncio.sleep(backoff)
                continue
            return await self._outcome_from_token_data(data)
        # Unreachable: the loop always returns, but keeps mypy happy.
        return ExchangeOutcome.NETWORK_FAILED

    async def _outcome_from_token_data(
        self, data: Any,
    ) -> ExchangeOutcome:
        """Turn a definitive token-endpoint response into an outcome."""
        if not isinstance(data, dict):
            return ExchangeOutcome.AUTH_FAILED
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            logger.error(
                "[GOGTokens] token response missing tokens: keys=%s",
                list(data.keys()),
            )
            return ExchangeOutcome.AUTH_FAILED
        saved = await self._save(access, refresh)
        return ExchangeOutcome.OK if saved else ExchangeOutcome.AUTH_FAILED

    async def _token_request(self, params: dict[str, str]) -> bool:
        """Token request (used by ``refresh_if_stale`` — no retry).

        Kept on the plain-bool contract: a refresh token is not
        single-use in the same fragile way and the caller drains again
        on the next stale check, so the transient/definitive split is
        unnecessary here.
        """
        url = f"{self._config.token_url}?{urllib.parse.urlencode(params)}"
        data = await fetch_json_get(
            url,
            user_agent=self._config.user_agent,
            timeout=15.0,
            log_prefix="[GOGTokens] token endpoint",
        )
        if not isinstance(data, dict):
            return False
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            logger.error(
                "[GOGTokens] token response missing tokens: keys=%s",
                list(data.keys()),
            )
            return False
        return await self._save(access, refresh)

_ = Any
