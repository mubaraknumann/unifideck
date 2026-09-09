"""HTTP helpers — SSL context builder + JSON GET wrapper.

Two small module-level helpers shared by ``library.py``, ``dlc.py``,
``updates.py`` and ``tokens/oauth.py``:

* ``build_ssl_context()`` — returns an ``ssl.SSLContext`` with the
  bundled CA cert chain (required because some Steam Deck OS versions
  ship with an outdated cert store that rejects GOG.com).
* ``fetch_json_get(url, headers)`` — async JSON GET with timeout and
  structured error reporting. Callers that need to distinguish a
  *transient* network failure (DNS/connect/timeout/reset — worth
  retrying) from a *definitive* HTTP response or bad body (retry
  pointless) opt in via ``raise_on_transient=True``; the retry loop
  itself lives in the caller (e.g. ``tokens/oauth.py``), not here.

Kept module-level (no class) because there's no state to encapsulate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from unifideck.core.net import ssl_ctx_permissive

_logger = logging.getLogger(__name__)

class TransientNetworkError(Exception):
    """Raised by ``fetch_json_get(raise_on_transient=True)`` on a retryable failure.

    Signals that the request failed for a *transient* reason — DNS
    resolution, connect, timeout, or connection reset — i.e. the
    request never received an HTTP response from the server. A
    definitive HTTP response (any status, including 4xx) or a
    malformed body is NOT transient and yields ``None`` instead, so
    callers must never retry those (an already-consumed OAuth code
    would just fail again).
    """

def build_ssl_context() -> ssl.SSLContext:
    """Build ssl context.

    Uses permissive verification because some Steam Deck OS
    versions ship with an outdated CA cert store that rejects
    ``auth.gog.com`` despite the cert being valid. Without
    this, every GOG auth attempt fails at the token-exchange
    step with ``CERTIFICATE_VERIFY_FAILED``.
    """
    return ssl_ctx_permissive("GOG OAuth — outdated Deck cert store")

async def fetch_json_get(
    url: str,
    *,
    bearer: str | None = None,
    user_agent: str,
    timeout: float = 15.0,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    extra_headers: Mapping[str, str] | None = None,
    log_prefix: str = "[GOGHttp]",
    raise_on_transient: bool = False,
) -> Any | None:
    """Fetch JSON get.

    Returns the parsed JSON body on HTTP 200, or ``None`` on a
    *definitive* failure: any non-200 HTTP status (incl. 4xx) or a
    malformed body — retrying those is pointless.

    A *transient* network failure (DNS/connect/timeout/reset — the
    request never reached the server) is where the two callers
    diverge: with ``raise_on_transient=True`` it raises
    :class:`TransientNetworkError` so the caller can retry; with the
    default ``False`` it logs and returns ``None`` (the historical
    behaviour, kept for the non-auth callers that do not retry).
    """
    headers: dict[str, str] = {"User-Agent": user_agent}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if extra_headers:
        headers.update(extra_headers)

    return await asyncio.to_thread(
        _fetch_json_get_sync,
        url,
        headers,
        timeout,
        log_prefix,
        raise_on_transient,
    )

def _fetch_json_get_sync(
    url: str,
    headers: dict[str, str],
    timeout: float,
    log_prefix: str,
    raise_on_transient: bool,
) -> Any | None:
    """Blocking body of :func:`fetch_json_get`, run in a worker thread.

    Extracted to module level (rather than a closure) so
    ``fetch_json_get`` stays under the function-length volumetry cap;
    behaviour is identical.
    """
    try:
        ctx = build_ssl_context()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(
            req,
            timeout=timeout,
            context=ctx,
        ) as response:
            if response.status != 200:
                _logger.warning(
                    "%s GET %s → HTTP %d",
                    log_prefix,
                    url,
                    response.status,
                )
                return None
            return json.loads(response.read().decode())
    # HTTPError first — it subclasses URLError but carries a real
    # HTTP status, so it is a definitive server response, never
    # transient. Must precede the URLError clause below.
    except urllib.error.HTTPError as e:
        _logger.warning(
            "%s GET %s → HTTP %d",
            log_prefix,
            url,
            e.code,
        )
        return None
    # Transport-level failure — the request never got an HTTP
    # response back (gaierror is an OSError; URLError wraps it).
    # This is the retryable case.
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _logger.warning(
            "%s GET %s failed (network): %s",
            log_prefix,
            url,
            e,
        )
        if raise_on_transient:
            raise TransientNetworkError(str(e)) from e
        return None
    # 200 with a body that isn't valid JSON — definitive, not a
    # network problem.
    except (json.JSONDecodeError, ValueError) as e:
        _logger.warning(
            "%s GET %s failed (bad body): %s",
            log_prefix,
            url,
            e,
        )
        return None
