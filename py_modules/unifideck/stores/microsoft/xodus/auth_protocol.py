"""stores/microsoft/xodus/auth_protocol.py — the Xodus sign-in protocol.

``xodus-cli --external-webview-stdio login`` asks its host to show web
pages and to relay what the page says back. The two talk in
``NEXUS_AUTH_V1 {json}`` lines (the CLI writes them on stdout; the host
answers on stdin with plain JSON lines)::

    cli  -> {"type":"ready","version":1}
    host -> {"type":"hello","version":1}
    cli  -> {"type":"open","session":N,"url":...,"headers":{...}}
    cli  -> {"type":"context","session":N,"context":...}
    host -> {"type":"ipc","session":N,"payload":"<page message>"}
    cli  -> {"type":"close","session":N}
    host -> {"type":"closed","session":N}       (the user closed the window)

The page side is the Windows CloudExperienceHost bridge: the sign-in page
calls ``window.external.notify(...)``. ``BRIDGE_JS`` gives the page that
function, but only on the top frame of a trusted Microsoft host.

A pure port of the spike's ``signin_host.py``. No I/O. Nothing here logs a
payload, a token, a cookie or a query string: only frame types, hosts and
paths.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from .cli_models import redact

PREFIX = "NEXUS_AUTH_V1 "
PROTOCOL_VERSION = 1
BINDING = "__unifideckXodusNotify"
TRUSTED_HOSTS = frozenset({
    "login.live.com", "account.live.com", "login.microsoftonline.com",
    "login.microsoft.com", "account.microsoft.com", "sisu.xboxlive.com",
})
#: The user agent of the Windows web host the backend expects.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; MSAppHost/3.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 Edge/18.26100"
)
CONTEXT_ARGS = [
    "CloudExperienceHost", "TokenBroker", "TokenBroker",
    '{"PrivatePropertyBag":1,"PasswordlessConnect":1,"PreferAssociate":1,"ChromelessUI":0}',
]
#: Only these requests are paused, so the backend's headers can be added.
FETCH_PATTERNS = [
    {"urlPattern": "https://login.live.com/ppsecure/*", "resourceType": "Document", "requestStage": "Request"},
]
#: The first leg of a sign-in.
INLINE_LOGIN_URL = "https://login.live.com/ppsecure/InlineLogin.srf"
#: ``xodus-cli`` sends the nine ``cxh-*`` headers with the first leg only (its
#: ``finalize_request`` passes an empty HeaderMap); xodus-nexus 0.2.11 fixed
#: that in the backend ("keep Microsoft broker headers on both sign-in
#: steps"). The bridge replays them on the later legs so the window behaves
#: like that fixed backend. On its own this did not end the second-leg loop
#: seen with backend 0.1.4, so treat it as parity with upstream, not a cure.
#: The header that names one request rather than the client. xodus-nexus
#: sends a new value on every leg.
CORRELATION_HEADER = "cxh-correlationid"
#: The page whose ``ServerData`` finishes the sign-in.
POST_SRF = ("login.live.com", "/ppsecure/post.srf")
#: A page message the bridge relays is at most this long.
MAX_PAYLOAD = 65536
#: Bridge methods the HOST has to answer: ``xodus-cli`` understands only
#: ``CloudExperienceHost.getContext`` (``HostBridgeMessage::get_context_invoke``
#: upstream) and drops every other invoke. The sign-in page awaits a callback
#: for these before it continues — ``handleOnPasswordUpdate`` chains
#: ``MSA.encryptUserData`` into ``Storage.PrivateData.addItem`` and only then
#: calls its continuation — so silence leaves the Sign in button inert and the
#: page just retries the invoke on each press.
_HOST_INVOKES = frozenset({
    "CloudExperienceHost.Storage.PrivateData.addItem",
    "CloudExperienceHost.Storage.PrivateData.getItem",
    "CloudExperienceHost.Storage.SharableData.addValue",
    "CloudExperienceHost.Storage.SharableData.getValue",
    "CloudExperienceHost.Storage.VolatileSharableData.addItem",
    "CloudExperienceHost.Storage.VolatileSharableData.getItem",
    "CloudExperienceHost.MSA.encryptUserData",
})
#: The one host call whose result the page stores verbatim (property bag
#: ``encPwd``). This host keeps no bag and never reads it back, so any
#: non-null string lets the continuation run.
_ENCRYPT_USER_DATA = "CloudExperienceHost.MSA.encryptUserData"
#: A ``CloudExperienceHost`` bridge method name, safe to log as-is.
_CXH_NAME_RE = re.compile(r"^CloudExperienceHost(\.[A-Za-z]{1,40}){1,4}$")

#: Runs before the page's own scripts, in the top frame of trusted hosts only.
BRIDGE_JS = """(() => {
  if (window !== window.top) return;
  const hosts = __HOSTS__;
  if (location.protocol !== 'https:' || location.port || !hosts.includes(location.hostname)) return;
  const send = globalThis.__BINDING__;
  if (typeof send !== 'function') return;
  try {
    Object.defineProperty(globalThis.external, 'notify', {
      value: payload => { if (typeof payload === 'string' && payload.length <= __MAX__) send(payload); },
      configurable: false, writable: false,
    });
  } catch (e) {}
})();""".replace("__HOSTS__", json.dumps(sorted(TRUSTED_HOSTS))).replace(
    "__BINDING__", BINDING,
).replace("__MAX__", str(MAX_PAYLOAD))

SERVER_DATA_EXPR = "JSON.stringify(globalThis.ServerData ?? null)"


def parse_frame(line: str) -> dict[str, Any] | None:
    """The frame on a CLI stdout line, or None for any other line."""
    text = line.rstrip("\r\n")
    if not text.startswith(PREFIX):
        return None
    try:
        frame = json.loads(text[len(PREFIX):])
    except ValueError:
        return None
    return frame if isinstance(frame, dict) and isinstance(frame.get("type"), str) else None


def encode(frame: dict[str, Any]) -> bytes:
    """One host frame as a stdin line."""
    return (json.dumps(frame) + "\n").encode()


def hello_frame() -> dict[str, Any]:
    """The host's answer to ``ready``."""
    return {"type": "hello", "version": PROTOCOL_VERSION}


def ipc_frame(session: int, payload: str) -> dict[str, Any]:
    """A page message for the backend."""
    return {"type": "ipc", "session": session, "payload": payload}


def closed_frame(session: int) -> dict[str, Any]:
    """The user closed the sign-in window."""
    return {"type": "closed", "session": session}


def is_trusted_url(url: str) -> bool:
    """An https URL on a trusted host with no explicit port."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and port is None and (parts.hostname or "") in TRUSTED_HOSTS


def is_post_srf(url: str) -> bool:
    """The page whose ``ServerData`` ends the sign-in."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname, parts.path) == POST_SRF


def safe_url(url: str) -> str:
    """Scheme, host and path only: queries can carry flow tokens."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparsable>"
    if parts.hostname:
        return f"{parts.scheme}://{parts.hostname}{parts.path}"
    return redact(url.split("?", 1)[0][:60])


def merge_cxh_headers(
    url: str, page_headers: dict[str, Any], pending: dict[str, str] | None,
    leg_url: str = INLINE_LOGIN_URL,
) -> list[dict[str, str]] | None:
    """Headers for ``Fetch.continueRequest``, or None to leave them as they are.

    Only the request for the leg's own page (``leg_url``, ignoring its query)
    gets the backend's headers, as in xodus-nexus. The caller clears
    ``pending`` once this returns a list.
    """
    if not pending or not url.startswith(leg_url.split("?", 1)[0]):
        return None
    headers = {str(k): str(v) for k, v in page_headers.items()}
    headers.update(pending)
    return [{"name": k, "value": v} for k, v in headers.items()]


def fresh_correlation(headers: dict[str, str], correlation_id: str) -> dict[str, str]:
    """``headers`` with ``cxh-correlationid`` replaced, the other eight kept.

    xodus-nexus sends a new correlation id with every leg's headers.
    """
    out = {k: v for k, v in headers.items() if k.lower() != CORRELATION_HEADER}
    out[CORRELATION_HEADER] = correlation_id
    return out


def context_dispatch_expr(context: Any) -> str:
    """JS that answers the page's ``CloudExperienceHost.getContext`` call."""
    message = json.dumps({"type": "callback", "value": {
        "name": "CloudExperienceHost.getContext", "args": CONTEXT_ARGS, "context": context,
    }})
    return f'globalThis["CloudExperienceHost.Bridge.dispatchMessage"]({json.dumps(message)})'


def invoke_answer(payload: str) -> tuple[str, str] | None:
    """``(method, js)`` answering one host contract call the backend ignores.

    None when the page message is not such a call. The page's bridge
    (``CloudExperienceHost.Bridge.prototype.invoke``) resolves its promise on
    ``value.context`` and takes the answer from ``value.result``;
    ``value.iserror`` rejects it. The context string is the page's own and is
    in the invoke payload, so no state is kept here.

    ``method`` is one of ``_HOST_INVOKES`` and safe to log. The payload itself
    is never logged or echoed (``encryptUserData`` carries the password in its
    args), and the caller must not forward it to the backend either.
    """
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "invoke":
        return None
    value = data.get("value")
    if not isinstance(value, dict):
        return None
    name, context = value.get("name"), value.get("context")
    if not isinstance(context, str) or name not in _HOST_INVOKES:
        return None
    result = "" if name == _ENCRYPT_USER_DATA else None
    message = json.dumps({"type": "callback", "value": {
        "context": context, "result": result, "iserror": False,
    }})
    # Guarded: the bridge may not exist yet if the page is mid-navigation.
    expr = (
        '((f, m) => typeof f === "function" && f(m))'
        f'(globalThis["CloudExperienceHost.Bridge.dispatchMessage"], {json.dumps(message)})'
    )
    return str(name), expr


def serverdata_ok(value: Any) -> bool:
    """Whether a ``ServerData`` read is worth relaying."""
    return isinstance(value, str) and value != "null" and len(value) <= MAX_PAYLOAD


def describe_notify(payload: str) -> str:
    """A log-safe summary of a page message. Never echoes a token."""
    try:
        data = json.loads(payload)
    except ValueError:
        return "non-JSON payload"
    if isinstance(data, dict) and "sDAToken" in data:
        return "token payload (redacted)"
    if isinstance(data, dict):
        raw = data.get("value")
        value = raw if isinstance(raw, dict) else {}
        name = value.get("name")
        # A bridge method name is not a secret, but it is long enough for
        # ``redact`` to take it for a token (I-061).
        shown = name if isinstance(name, str) and _CXH_NAME_RE.match(name) else redact(f"{name}")
        return f"type={redact(str(data.get('type')))[:40]} name={shown}"[:120]
    return "non-object payload"
