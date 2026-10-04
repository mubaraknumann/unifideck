import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from unifideck.core.net import ssl_ctx_permissive as _ssl

logger = logging.getLogger(__name__)
__all__ = [
    "describe_xerr",
    "http_get",
    "http_post",
    "request_xsts_token",
]
def http_post(url: str, data: dict[str, Any], headers: dict[str, Any]) -> dict[str, Any]:
    """Http post."""
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=_ssl("Microsoft OAuth — outdated Deck cert store")) as r:
        return cast(dict[str, Any], json.loads(r.read().decode()))
def http_post_json(url: str, payload: dict[str, Any], headers: dict[str, Any]) -> dict[str, Any]:
    """Http post JSON."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=_ssl("Microsoft OAuth — outdated Deck cert store")) as r:
        return cast(dict[str, Any], json.loads(r.read().decode()))
def http_get(url: str, headers: dict[str, Any]) -> dict[str, Any]:
    """Http get."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15, context=_ssl("Microsoft OAuth — outdated Deck cert store")) as r:
        return cast(dict[str, Any], json.loads(r.read().decode()))

def request_xsts_token(
    xbl_token: str,
    xsts_rp: str,
    locale: str,
    xsts_url: str,
    xbl_user_agent: str,
) -> dict[str, Any] | None:
    """Request XSTS token."""
    return _request_xsts_token(
        xbl_token, xsts_rp, locale, xsts_url, xbl_user_agent,
    )

def _obtain_xbl_user_token(
    access_token: str,
    locale: str,
    xbl_auth_url: str,
    xbl_user_agent: str,
    *,
    prefer_d: bool = False,
) -> dict[str, Any] | None:
    """Exchange an MSA access token for an XBL user token.

    The RpsTicket prefix depends on who issued the access token: ``t=``
    (contract v2) for a login.live.com ``MBI_SSL`` ticket, ``d=`` (contract
    v1) for an Azure AD v2 ``XboxLive.signin`` token such as xbox.com's
    device-code client. The likely one goes first so a sign-in costs one
    request, not a failed one plus a retry, on every relying party.
    """
    live = [("2", f"t={access_token}"), ("1", f"d={access_token}"), ("1", f"t={access_token}")]
    aad = [("1", f"d={access_token}"), ("2", f"t={access_token}")]
    candidates = aad if prefer_d else live
    for contract_v, rps in candidates:
        resp = _try_xbl_request(
            contract_v, rps, locale, xbl_auth_url, xbl_user_agent,
        )
        if resp is not None and resp.get("Token"):
            logger.info(
                "[MS] XBL auth OK (contract-v%s, prefix=%r)",
                contract_v, rps[:2],
            )
            return resp
    logger.error(
        "[MS] XBL user token failed with all contract/prefix combos",
    )
    return None
def _try_xbl_request(
    contract_v: str,
    rps: str,
    locale: str,
    xbl_auth_url: str,
    xbl_user_agent: str,
) -> dict[str, Any] | None:
    """Try XBL request."""
    body = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": rps,
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": contract_v,
        "User-Agent": xbl_user_agent,
        "Accept-Language": locale,
    }
    try:
        return http_post_json(xbl_auth_url, body, headers)
    except urllib.error.HTTPError as e:
        body_text = _read_http_error_body(e)
        logger.debug(
            "[MS] XBL failed (v%s, %r): HTTP %d %s",
            contract_v, rps[:2], e.code, body_text[:500],
        )
        return None
    except Exception as e:
        logger.debug(
            "[MS] XBL failed (v%s, %r): %s",
            contract_v, rps[:2], e,
        )
        return None
def _extract_user_hash(xbl_resp: dict[str, Any]) -> str | None:
    """Extract user hash."""
    display_claims = xbl_resp.get("DisplayClaims", {})
    xui = display_claims.get("xui", [{}])
    return xui[0].get("uhs") if xui else None

def _request_xsts_token(
    xbl_token: str,
    xsts_rp: str,
    locale: str,
    xsts_url: str,
    xbl_user_agent: str,
) -> dict[str, Any] | None:

    """Request XSTS token."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": "1",
        "User-Agent": xbl_user_agent,
        "Accept-Language": locale,
    }
    body = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": xsts_rp,
        "TokenType": "JWT",
    }
    try:
        resp = http_post_json(xsts_url, body, headers)
        logger.info(
            "[MS] ✓ XSTS obtained with RP=%r sandbox='RETAIL'",
            xsts_rp,
        )
        return resp
    except urllib.error.HTTPError as e:
        body_text = _read_http_error_body(e)
        logger.warning(
            "[MS] XSTS failed (RP=%r): HTTP %d %s",
            xsts_rp, e.code, body_text[:500],
        )
        return None
    except Exception as e:
        logger.warning(
            "[MS] XSTS failed (RP=%r): %s", xsts_rp, e,
        )
        return None
#: XSTS ``XErr`` codes, as Microsoft documents them for Xbox Live sign-in.
_XERR_MEANINGS: dict[int, str] = {
    2148916227: "account banned from Xbox",
    2148916229: "account restricted by guardian settings",
    2148916233: "account has no Xbox profile (create one at xbox.com)",
    2148916234: "Xbox terms of service not accepted",
    2148916235: "Xbox is not available in the account's region",
    2148916236: "account needs adult verification",
    2148916237: "account needs adult verification",
    2148916238: "child account must be added to a family by an adult",
}


def describe_xerr(xerr: int) -> str:
    """A readable meaning for an XSTS ``XErr`` code."""
    return _XERR_MEANINGS.get(xerr, f"unknown XErr {xerr}")


def _read_http_error_body(err: urllib.error.HTTPError) -> str:
    """Read http error body."""
    try:
        return err.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
