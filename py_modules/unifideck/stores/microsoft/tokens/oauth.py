from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .endpoint import TokenState, classify_token_reply, post_form

if TYPE_CHECKING:
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
logger = logging.getLogger(__name__)

#: After a refresh that could not reach Microsoft, answer TRANSIENT from
#: memory for this long instead of retrying. Every QAM status check calls
#: ``refresh_if_stale``; offline, each retry would block it for the full
#: request timeout.
TRANSIENT_QUIET_SECONDS = 60.0


class OAuthMixin:
    """Oauth mixin."""
    _ms_access_token: str | None
    _ms_refresh_token: str | None
    _token_saved_at: float
    _config: MicrosoftConfig
    _refresh_lock: asyncio.Lock
    _transient_until: float
    _refresh_error: str

    async def exchange_code(self, auth_code: str) -> bool:
        """Exchange code."""
        state = await self._token_request({
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "code": auth_code,
            "grant_type": "authorization_code",
            "scope": self._config.scope,
        })
        return state is TokenState.FRESH

    def _access_token_is_fresh(self) -> bool:
        age = time.time() - self._token_saved_at
        return bool(self._ms_access_token) and (
            age < self._config.token_refresh_threshold_seconds
        )

    async def refresh_if_stale(self) -> TokenState:
        """Refresh the access token when it is due; say what that meant.

        Only ``DEAD`` means the sign-in is gone. ``TRANSIENT`` means
        Microsoft could not be reached: keep the tokens, retry later.
        One refresh runs at a time, because Microsoft rotates the refresh
        token on every refresh and two racing refreshes would save one
        token the server already replaced.
        """
        if self._access_token_is_fresh():
            return TokenState.FRESH
        if not self._ms_refresh_token:
            logger.error(
                "[MicrosoftTokens] refresh needed but no "
                "refresh token available — session dead",
            )
            self._refresh_error = "no_refresh_token"
            return TokenState.DEAD
        async with self._refresh_lock:
            if self._access_token_is_fresh():
                return TokenState.FRESH  # another caller just refreshed
            if time.monotonic() < self._transient_until:
                return TokenState.TRANSIENT
            logger.info(
                "[MicrosoftTokens] refreshing access token (age=%.0fs)",
                time.time() - self._token_saved_at,
            )
            params = {
                "client_id": self._config.client_id,
                "refresh_token": self._ms_refresh_token,
                "grant_type": "refresh_token",
                "scope": self._config.scope,
            }
            if self._config.redirect_uri:  # the browser-redirect client only
                params["redirect_uri"] = self._config.redirect_uri
            state = await self._token_request(params)
            if state is TokenState.TRANSIENT:
                self._transient_until = time.monotonic() + TRANSIENT_QUIET_SECONDS
            return state

    async def _token_request(self, params: dict[str, str]) -> TokenState:
        """POST to the token endpoint; on success store and save the tokens."""
        url = self._config.token_url
        reply = await asyncio.to_thread(post_form, url, params)
        state, reason = classify_token_reply(reply)
        if state is TokenState.TRANSIENT:
            logger.warning(
                "[MicrosoftTokens] could not refresh the Microsoft sign-in "
                "at %s (%s); keeping it and retrying later",
                url, reason,
            )
            self._refresh_error = reason
            return state
        if state is TokenState.DEAD:
            logger.error(
                "[MicrosoftTokens] %s rejected the Microsoft sign-in (%s); "
                "the user must sign in again",
                url, reason,
            )
            self._refresh_error = reason
            return state
        await self.accept_token_body(reply.body)
        return TokenState.FRESH

    async def accept_token_body(self, body: dict[str, Any]) -> None:
        """Store and save a successful token response.

        Microsoft rotates the refresh token on every refresh, so the new one
        must replace the old one before anything else uses it.
        """
        self._ms_access_token = body["access_token"]
        new_refresh = body.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh:
            self._ms_refresh_token = new_refresh
        self._token_saved_at = time.time()
        self._transient_until = 0.0
        self._refresh_error = ""
        await self.save()  # type: ignore[attr-defined]  # self.save provided by sibling mixin _PersistenceMixin
