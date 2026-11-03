"""stores/microsoft/xbox_com_grant.py — the saved Microsoft sign-in, as xbox.com's own token answer.

Xbox Cloud Gaming streams on xbox.com's own sign-in in the Edge profile,
not on Unifideck's tokens. Since 0.7.6 the device-code sign-in uses
xbox.com's own client (``XBOX_COM_CLIENT_ID``) on the same authority, so its
refresh token is one xbox.com would have issued for itself. Refreshing it
with xbox.com's scope set (``openid`` added, ``client_info=1``) returns the
answer xbox.com's device-code page expects, and
``launcher/flows/xbox_com_signin`` hands it to the page. The user never signs
in to xbox.com separately.

Read-only by design: it runs in the launcher, beside the backend's own
refresh loop, so it never writes the token file. Microsoft keeps a refresh
token valid after it is used (measured 2026-11-03), so the refresh here
does not invalidate the backend's copy.

Only a sign-in issued to ``XBOX_COM_CLIENT_ID`` can be handed over. Any other
(an older client, or none at all) gets ``None`` with the reason, and the
stream window shows xbox.com's own sign-in instead.
Token values are never logged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unifideck.security import SecureTokenStore

from .tokens.endpoint import post_form

#: xbox.com's own MSAL client (also Unifideck's device-code client since 0.7.6).
XBOX_COM_CLIENT_ID = "1f907974-e22b-4810-a9de-d9647380c97e"
#: The authority xbox.com's MSAL signs in against.
XBOX_COM_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"  # noqa: S105 — an endpoint, not a credential
#: The scopes xbox.com's own device-code flow requests.
XBOX_COM_SCOPE = "xboxlive.signin offline_access openid"
#: Default token file (``microsoft.token_file`` in defaults/config.json).
DEFAULT_TOKEN_FILE = "~/.config/unifideck/microsoft_tokens.json"  # noqa: S105 — filename constant, not a credential


@dataclass(frozen=True)
class XboxComGrant:
    """A token answer for xbox.com, or why there is none."""

    body: dict[str, Any] = field(default_factory=dict, repr=False)
    reason: str = ""

    @property
    def ok(self) -> bool:
        """True when ``body`` holds a usable token answer."""
        return bool(self.body.get("access_token") and self.body.get("refresh_token"))


def saved_refresh_token(path: Path) -> tuple[str, str]:
    """``(refresh_token, "")`` from the saved sign-in, or ``("", why not)``."""
    if not path.is_file():
        return "", f"no saved Microsoft sign-in at {path}"
    try:
        data = SecureTokenStore().decrypt_payload(path.read_bytes())
    except Exception as e:
        return "", f"could not read the saved sign-in at {path} ({type(e).__name__})"
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return "", f"the saved sign-in at {path} has no refresh token"
    client = data.get("client_id")
    if client != XBOX_COM_CLIENT_ID:
        return "", (
            f"the saved sign-in at {path} was issued to client {client or 'unrecorded'}, "
            "which xbox.com does not accept"
        )
    return refresh, ""


def load_xbox_com_grant(token_file: str = DEFAULT_TOKEN_FILE) -> XboxComGrant:
    """Refresh the saved sign-in into a token answer for xbox.com. Never raises.

    Blocking (file read + one HTTPS POST): call it through ``asyncio.to_thread``.
    """
    path = Path(token_file or DEFAULT_TOKEN_FILE).expanduser()
    refresh, why = saved_refresh_token(path)
    if not refresh:
        return XboxComGrant(reason=why)
    reply = post_form(XBOX_COM_TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": XBOX_COM_CLIENT_ID,
        "refresh_token": refresh,
        "scope": XBOX_COM_SCOPE,
        "client_info": "1",
    })
    grant = XboxComGrant(body=dict(reply.body))
    if reply.status == 200 and grant.ok:
        return grant
    return XboxComGrant(reason=(
        f"Microsoft did not refresh the saved sign-in for xbox.com "
        f"(HTTP {reply.status}, {reply.error or reply.detail or 'no token in the answer'})"
    ))
