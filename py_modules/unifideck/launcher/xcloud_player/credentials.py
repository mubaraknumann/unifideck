"""The two xCloud credentials, from the plugin's saved OAuth sign-in.

* streaming token (``gsToken``) + region: refresh the saved sign-in, then
  Xbox Live user token (``d=``), the ``gssv`` XSTS, and the xgpuweb login;
* console-transfer ticket for ``/connect``: the same refresh token, with the
  purpose scope xCloud's console asks for. Only an OAuth refresh token can mint
  it; the Xbox PC credential is refused (``missing field wst:TokenType``).

Read-only: Microsoft keeps a refresh token valid after it is used, so nothing
is written back beside the backend's own refresh loop. Blocking (HTTPS): call
through ``asyncio.to_thread``. Token values are never logged.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unifideck.stores.microsoft.microsoft_auth import (
    _extract_user_hash,
    _obtain_xbl_user_token,
    request_xsts_token,
)
from unifideck.stores.microsoft.microsoft_subscription import _probe_sync
from unifideck.stores.microsoft.tokens.endpoint import post_form
from unifideck.stores.microsoft.xbox_com_grant import (
    DEFAULT_TOKEN_FILE,
    XBOX_COM_CLIENT_ID,
    XBOX_COM_TOKEN_URL,
    saved_refresh_token,
)

XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
LOGIN_URL = "https://xgpuweb.gssv-play-prod.xboxlive.com/v2/login/user"
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"  # noqa: S105 — an endpoint
TRANSFER_SCOPE = "service::http://Passport.NET/purpose::PURPOSE_XBOX_CLOUD_CONSOLE_TRANSFER_TOKEN"
_XBL_UA = "XboxReplay; XboxLiveAuth/3.0"
#: Wait before asking for the token a second time.
XSTS_RETRY_DELAY_S = 2.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamLogin:
    """What a stream needs from the sign-in, or why there is none."""

    gs_token: str = field(default="", repr=False)
    base_uri: str = ""
    refresh_token: str = field(default="", repr=False)
    reason: str = ""

    @property
    def ok(self) -> bool:
        """True when a session can be started."""
        return bool(self.gs_token and self.base_uri and self.refresh_token)


def _default_region(regions: list[dict[str, Any]]) -> str:
    chosen = next((r for r in regions if r.get("isDefault")), regions[0] if regions else {})
    return str(chosen.get("baseUri") or "")


def _request_gssv_xsts(user_token: str) -> dict[str, Any] | None:
    return request_xsts_token(xbl_token=user_token, xsts_rp="http://gssv.xboxlive.com/",
                              locale="en-US", xsts_url=XSTS_URL, xbl_user_agent=_XBL_UA)


def _gssv_xsts(access_token: str) -> tuple[str, str] | str:
    """``(user_hash, xsts)`` for xCloud, or why not."""
    user = _obtain_xbl_user_token(access_token, "en-US", XBL_URL, _XBL_UA, prefer_d=True)
    if not user or not user.get("Token"):
        return "Xbox Live refused the saved Microsoft sign-in"
    xsts = _request_gssv_xsts(user["Token"])
    if xsts is None:
        logger.info("[xcloud_player] no xCloud token on the first try; asking once more")
        time.sleep(XSTS_RETRY_DELAY_S)
        xsts = _request_gssv_xsts(user["Token"])
    if xsts is None:
        # ``request_xsts_token`` has logged whether it was a network fault or an
        # HTTP error. Only an answer that carries an XErr is a refusal.
        return "Xbox Live gave no xCloud token (network or service error, see the line above)"
    if not xsts.get("Token"):
        return f"Xbox Cloud Gaming refused this account (XErr {xsts.get('XErr')})"
    return _extract_user_hash(user) or "", str(xsts["Token"])


def stream_login(token_file: str = DEFAULT_TOKEN_FILE) -> StreamLogin:
    """Sign in to xCloud from the saved OAuth sign-in. Never raises."""
    path = Path(token_file or DEFAULT_TOKEN_FILE).expanduser()
    refresh, why = saved_refresh_token(path)
    if not refresh:
        return StreamLogin(reason=why)
    reply = post_form(XBOX_COM_TOKEN_URL, {
        "grant_type": "refresh_token", "client_id": XBOX_COM_CLIENT_ID,
        "refresh_token": refresh, "scope": "XboxLive.signin offline_access",
    })
    access = reply.body.get("access_token")
    if reply.status != 200 or not isinstance(access, str):
        return StreamLogin(reason=f"Microsoft did not refresh the saved sign-in "
                                  f"(HTTP {reply.status}, {reply.error or reply.detail})")
    chain = _gssv_xsts(access)
    if isinstance(chain, str):
        return StreamLogin(reason=chain)
    probe = _probe_sync(chain[0], chain[1], LOGIN_URL, 20, "xgpuweb")
    if not probe.gs_token:
        return StreamLogin(reason=f"the Xbox Cloud Gaming login failed ({probe.error})")
    return StreamLogin(gs_token=probe.gs_token, base_uri=_default_region(probe.regions),
                       refresh_token=refresh)


def transfer_ticket(refresh_token: str) -> str:
    """The console-transfer ticket ``/connect`` wants, or ``""``. Never raises."""
    reply = post_form(LIVE_TOKEN_URL, {
        "client_id": XBOX_COM_CLIENT_ID, "scope": TRANSFER_SCOPE,
        "grant_type": "refresh_token", "refresh_token": refresh_token,
    })
    ticket = reply.body.get("access_token")
    return ticket if reply.status == 200 and isinstance(ticket, str) else ""
