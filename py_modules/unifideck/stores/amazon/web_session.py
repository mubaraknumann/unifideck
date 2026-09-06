"""stores/amazon/web_session.py — a signed-in amazon.com in the browser.

Amazon is the one store whose sign-in leaves the shared Edge profile
without a usable web session. nile authenticates through Amazon's
*device registration* flow: the user signs in at a page whose whole
purpose is to authorise a device and hand back tokens, and it ends on
``amazon.com/ap/maplanding`` carrying the access token in the URL.
That registers the device, but the browser is left holding only
tracking cookies. Measured on the profile: 11 amazon.com cookies
present, of which the auth ones (``at-main``, ``sess-at-main``,
``x-main``) were all absent — which is exactly why the shop opened
logged out while nile itself worked fine.

Amazon's own first-party apps handle this by exchanging a refresh
token for website cookies, and the endpoint is available to us with
the token nile already holds:

    POST https://api.amazon.com/auth/token
    { requested_token_type: "auth_cookies",
      domain: ".amazon.com",
      source_token_type: "refresh_token",
      source_token: <nile's refresh token> }

Verified against a live account: returns ``at-main``, ``sess-at-main``,
``session-id``, ``ubid-main`` and ``x-main`` — a complete session.
Writing them into the Edge profile is
``auth.edge_browser.cookie_writer``'s job.

This buys a signed-in **amazon.com**, from which Prime Gaming and Luna
are one tap away in the site nav. It does not attempt to mint a session
for ``gaming.amazon.com`` directly — that host did not accept the
parent-domain cookie when tried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from unifideck.core.net import ssl_ctx_permissive

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.amazon.com/auth/token"  # noqa: S105 — an endpoint, not a credential
_COOKIE_DOMAIN = ".amazon.com"
# The registered client nile presents. Amazon keys the exchange on the
# app the refresh token was issued to, so this must match nile's.
_APP_NAME = "AGSLauncher for Windows"
_TIMEOUT_S = 20

# nile's own credential file. Read directly rather than through the
# store's token manager: the plugin keeps its own encrypted copy, but
# the refresh token Amazon will accept for this exchange is the one
# nile registered with.
_NILE_USER_FILE = Path("~/.config/nile/user.json").expanduser()


def _read_refresh_token() -> str:
    """nile's Amazon refresh token, or ``""`` when unavailable."""
    try:
        data = json.loads(_NILE_USER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.info("[AmazonWebSession] no nile credentials: %s", e)
        return ""
    token = (
        data.get("tokens", {}).get("bearer", {}).get("refresh_token")
        if isinstance(data, dict)
        else None
    )
    return str(token or "")


def _exchange_sync(refresh_token: str) -> list[dict[str, Any]]:
    """Blocking half of the exchange. Returns raw cookie dicts."""
    body = json.dumps({
        "requested_token_type": "auth_cookies",
        "app_name": _APP_NAME,
        "domain": _COOKIE_DOMAIN,
        "source_token": refresh_token,
        "source_token_type": "refresh_token",
    }).encode()
    request = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AGSLauncher/1.0.0",
        },
    )
    ctx = ssl_ctx_permissive("Amazon cookie exchange — outdated Deck cert store")
    with urllib.request.urlopen(
        request, timeout=_TIMEOUT_S, context=ctx,
    ) as response:
        payload = json.loads(response.read())
    by_domain = (
        payload.get("response", {}).get("tokens", {}).get("cookies", {})
    )
    out: list[dict[str, Any]] = []
    for domain, entries in by_domain.items():
        for entry in entries or []:
            name = entry.get("Name")
            value = entry.get("Value")
            if not name or value is None:
                continue
            out.append({
                "host": domain or _COOKIE_DOMAIN,
                "name": name,
                "value": value,
                "path": entry.get("Path") or "/",
                "secure": bool(entry.get("Secure", True)),
                "httponly": bool(entry.get("HttpOnly", False)),
            })
    return out


async def fetch_website_cookies() -> list[dict[str, Any]]:
    """Amazon website cookies for the signed-in account, or ``[]``.

    Never raises. Every failure path — no nile credentials, a rejected
    or expired refresh token, no network — answers ``[]``, and the shop
    then opens with whatever session the profile already had. Losing
    the convenience must never cost the user the button.
    """
    refresh_token = await asyncio.to_thread(_read_refresh_token)
    if not refresh_token:
        return []
    try:
        cookies = await asyncio.to_thread(_exchange_sync, refresh_token)
    except urllib.error.HTTPError as e:
        logger.warning(
            "[AmazonWebSession] cookie exchange rejected: HTTP %s", e.code,
        )
        return []
    except Exception as e:
        logger.warning("[AmazonWebSession] cookie exchange failed: %s", e)
        return []
    logger.info(
        "[AmazonWebSession] exchanged refresh token for %d cookie(s)",
        len(cookies),
    )
    return cookies
