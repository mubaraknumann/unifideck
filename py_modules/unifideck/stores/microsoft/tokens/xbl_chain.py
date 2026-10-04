from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from unifideck.stores.microsoft.microsoft_auth import (
    _extract_user_hash,
    _obtain_xbl_user_token,
    describe_xerr,
    request_xsts_token,
)

if TYPE_CHECKING:
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
logger = logging.getLogger(__name__)

#: The relying party whose XSTS token carries the user's xuid.
XBOXLIVE_RELYING_PARTY = "http://xboxlive.com"
#: Re-mint the XBL user token this long before Microsoft's ``NotAfter``.
_USER_TOKEN_MARGIN_SECONDS = 300.0
#: Assumed lifetime when the response carries no parsable ``NotAfter``.
_USER_TOKEN_FALLBACK_SECONDS = 3600.0


@dataclass
class XBLTokenChain:
    """Xbltoken chain."""
    xsts_token: str
    user_hash: str
    xuid: str | None = None
    xbl_token: str | None = None


@dataclass(frozen=True)
class _UserToken:
    token: str
    user_hash: str
    expires_at: float
    access_token: str


class XBLChainMixin:
    """XBL user token + XSTS tokens for any relying party.

    One XBL user token serves every relying party (``xboxlive.com`` for the
    xuid, ``gssv`` for xCloud, ``mp.microsoft.com`` for the Store). It is
    cached until shortly before its ``NotAfter`` and tied to the access
    token it came from, so a sign-out or a new sign-in never reuses it.
    """
    _ms_access_token: str | None
    _config: MicrosoftConfig
    _locale_fn: Callable[[], str]
    _xbl_user: _UserToken | None
    #: XErr of the last refused XSTS request, or None. Lets a caller say *why*
    #: Xbox refused (no profile, child account, region) instead of "failed".
    last_xerr: int | None = None

    async def build_chain(self) -> XBLTokenChain | None:
        """XSTS for ``xboxlive.com`` (carries the xuid)."""
        return await self.build_rp_chain(XBOXLIVE_RELYING_PARTY)

    async def build_gssv_chain(
        self,
        xbl_token: str | None = None,
    ) -> XBLTokenChain | None:
        """XSTS for xCloud streaming (``gssv``)."""
        return await self.build_rp_chain(self._config.gssv_relying_party, xbl_token)

    async def build_marketplace_chain(
        self,
        xbl_token: str | None = None,
    ) -> XBLTokenChain | None:
        """XSTS for the Microsoft Store (``mp.microsoft.com``)."""
        return await self.build_rp_chain(
            self._config.marketplace_relying_party, xbl_token,
        )

    async def build_rp_chain(
        self,
        relying_party: str,
        xbl_token: str | None = None,
    ) -> XBLTokenChain | None:
        """XSTS for ``relying_party``, from ``xbl_token`` or the cached user token."""
        if xbl_token is None:
            user = await self._xbl_user_token()
            if user is None:
                return None
            xbl_token = user.token
        loop = asyncio.get_event_loop()
        token = xbl_token
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: request_xsts_token(
                    xbl_token=token,
                    xsts_rp=relying_party,
                    locale=self._locale_fn(),
                    xsts_url=self._config.xsts_url,
                    xbl_user_agent=self._config.xbl_user_agent,
                ),
            )
        except Exception:
            logger.exception("[MicrosoftTokens] XSTS error (rp=%s)", relying_party)
            return None
        xerr = resp.get("XErr") if isinstance(resp, dict) else None
        self.last_xerr = xerr if isinstance(xerr, int) else None
        return _chain_from_xsts(resp, relying_party, token)

    async def _xbl_user_token(self) -> _UserToken | None:
        access_token = self._ms_access_token
        if not access_token:
            return None
        cached = self._xbl_user
        if (
            cached is not None
            and cached.access_token == access_token
            and time.time() < cached.expires_at - _USER_TOKEN_MARGIN_SECONDS
        ):
            return cached
        prefer_d = self._config.uses_device_code
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _obtain_xbl_user_token(
                    access_token,
                    self._locale_fn(),
                    self._config.xbl_auth_url,
                    self._config.xbl_user_agent,
                    prefer_d=prefer_d,
                ),
            )
        except Exception:
            logger.exception("[MicrosoftTokens] XBL user token error")
            return None
        if not resp or not resp.get("Token"):
            return None
        user_hash = _extract_user_hash(resp)
        if not user_hash:
            logger.error("[MicrosoftTokens] XBL user token has no user hash")
            return None
        self._xbl_user = _UserToken(
            token=resp["Token"],
            user_hash=user_hash,
            expires_at=_not_after(resp),
            access_token=access_token,
        )
        return self._xbl_user


def _chain_from_xsts(
    resp: dict[str, Any] | None, relying_party: str, xbl_token: str,
) -> XBLTokenChain | None:
    if not resp:
        logger.error("[MicrosoftTokens] XSTS empty response (rp=%s)", relying_party)
        return None
    if "XErr" in resp:
        xerr = resp.get("XErr")
        logger.error(
            "[MicrosoftTokens] XSTS refused (rp=%s, XErr=%s): %s",
            relying_party, xerr,
            describe_xerr(xerr) if isinstance(xerr, int) else "unknown",
        )
        return None
    xsts_token = resp.get("Token")
    claims = (resp.get("DisplayClaims") or {}).get("xui") or [{}]
    user_hash = claims[0].get("uhs")
    if not xsts_token or not user_hash:
        logger.error(
            "[MicrosoftTokens] XSTS missing token or user hash (rp=%s)",
            relying_party,
        )
        return None
    logger.info("[MicrosoftTokens] ✓ XSTS for %s (uhs=%s)", relying_party, user_hash)
    return XBLTokenChain(
        xsts_token=xsts_token,
        user_hash=user_hash,
        xuid=claims[0].get("xid"),
        xbl_token=xbl_token,
    )


def _not_after(resp: dict[str, Any]) -> float:
    raw = resp.get("NotAfter")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + _USER_TOKEN_FALLBACK_SECONDS
