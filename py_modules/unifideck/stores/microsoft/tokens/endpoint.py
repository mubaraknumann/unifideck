"""The Microsoft token endpoint: one form POST that never raises, and what it meant.

The old path (``microsoft_auth.http_post``) raised on any HTTP error, and the
caller turned every exception into ``False``. A Deck with no network and a
refresh token Microsoft had revoked were the same answer, and both signed
the user out. An offline Deck lost its Microsoft sign-in on the next refresh.

``classify_token_reply`` keeps three answers apart:

- ``FRESH``: a usable access token came back.
- ``TRANSIENT``: we could not get an answer (network, 5xx, 429, a captive
  portal's HTML). The session may be fine: keep it and try again later.
- ``DEAD``: Microsoft answered with an OAuth error. The refresh token or the
  client is rejected, and only a new sign-in fixes it.

Only a JSON body with an OAuth ``error`` field can make a session ``DEAD``.
Anything Microsoft did not clearly say counts as ``TRANSIENT``, because a
wrong ``DEAD`` signs the user out and a wrong ``TRANSIENT`` only retries.
"""
from __future__ import annotations

import enum
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from unifideck.core.net import ssl_ctx_strict


class TokenState(enum.Enum):
    """What a token request means for the stored sign-in."""

    FRESH = "fresh"
    TRANSIENT = "transient"
    DEAD = "dead"


@dataclass(frozen=True)
class HttpReply:
    """The raw answer: HTTP status (``None`` when no answer arrived) and JSON object body."""

    status: int | None
    body: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def error(self) -> str:
        """The OAuth ``error`` code, or ``""``."""
        value = self.body.get("error")
        return value if isinstance(value, str) else ""


#: OAuth errors that mean the refresh token itself is no longer accepted.
_DEAD_GRANT_ERRORS = frozenset({
    "invalid_grant", "interaction_required", "consent_required", "login_required",
})
#: OAuth errors that mean Microsoft no longer accepts this client.
_CLIENT_ERRORS = frozenset({"invalid_client", "unauthorized_client"})
#: Statuses that are about load or time, never about the token.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})


def post_form(url: str, params: dict[str, str], timeout: float = 15.0) -> HttpReply:
    """POST ``params`` form-encoded to ``url``. Never raises."""
    return _send(urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    ), timeout)


def request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Any = None,
    timeout: float = 20.0,
) -> HttpReply:
    """Send a JSON request (``payload`` as the body, if any). Never raises."""
    data = json.dumps(payload).encode() if payload is not None else None
    all_headers = {"Accept": "application/json", **headers}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    return _send(
        urllib.request.Request(url, data=data, headers=all_headers, method=method),
        timeout,
    )


def _send(request: urllib.request.Request, timeout: float) -> HttpReply:
    try:
        with urllib.request.urlopen(
            request, timeout=timeout,
            context=ssl_ctx_strict(),
        ) as response:
            return HttpReply(response.status, _json_object(response.read()))
    except urllib.error.HTTPError as e:
        return HttpReply(e.code, _json_object(_read_quietly(e)))
    except (OSError, ValueError) as e:
        # URLError, timeouts and connection resets are all OSError.
        return HttpReply(None, detail=f"{type(e).__name__}: {e}")


def classify_token_reply(reply: HttpReply) -> tuple[TokenState, str]:
    """``(state, reason)`` for an access-token request. ``reason`` is "" when fresh."""
    if reply.status is None:
        return TokenState.TRANSIENT, f"no answer ({reply.detail})"
    if reply.status == 200:
        if isinstance(reply.body.get("access_token"), str):
            return TokenState.FRESH, ""
        return TokenState.TRANSIENT, "200 without an access token"
    if reply.status in _RETRYABLE_STATUSES or reply.status >= 500:
        return TokenState.TRANSIENT, f"HTTP {reply.status}"
    error = reply.error
    if not error:
        return TokenState.TRANSIENT, f"HTTP {reply.status} without an OAuth error"
    if error in _CLIENT_ERRORS:
        return TokenState.DEAD, f"client_rejected ({error})"
    if error in _DEAD_GRANT_ERRORS:
        return TokenState.DEAD, error
    if error == "temporarily_unavailable":
        return TokenState.TRANSIENT, error
    return TokenState.DEAD, error


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_quietly(err: urllib.error.HTTPError) -> bytes:
    try:
        return err.read()
    except OSError:
        return b""
