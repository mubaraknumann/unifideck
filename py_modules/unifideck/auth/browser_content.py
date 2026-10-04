"""auth/browser_content.py — CDP-based page content extraction.

Extracted from ``auth/browser.py`` in lot 13a (file-cap split):
holds everything related to reading the auth code from page body
(rather than the URL). Two stores need this path:

* **Epic** — the ``/id/api/redirect`` intermediate page contains
  the code inside a JSON blob (``authorizationCode``) rather
  than the redirect URL itself.
* **Generic stores**: callers pass a ``ContentCapture`` to
  ``wait_for_redirect`` when they know their provider embeds the
  code in the page (itch.io also overrides the JS ``expression``,
  because its key is hidden from ``innerText``).

All functions here are module-scope (not methods) so this layer
is straightforward to test with mock targets: pass a dict, get
an ``AuthCaptureResult`` or ``None``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from .browser_types import INNER_TEXT_EXPRESSION, AuthCaptureResult, ContentCapture

logger = logging.getLogger(__name__)


def log_extract(first_attempt: bool, fmt: str, *args: Any) -> None:
    """Log content-extraction events at INFO on the first attempt, DEBUG after.

    The page body for Epic's ``/id/api/redirect`` mutates
    during login — first checks always fail (login form,
    no code yet), so failure on the first attempt is
    diagnostic. Subsequent retries are routine polling
    chatter and belong at DEBUG.
    """
    level = logger.info if first_attempt else logger.debug
    level("[auth/browser] " + fmt, *args)


def match_pattern_in_text(
    text: str,
    pattern: str,
    url_snippet: str,
    first_attempt: bool,
) -> str | None:
    """Apply ``pattern`` to ``text``, return the first capture group.

    Logs a miss at INFO/DEBUG (per ``first_attempt``)
    including the body length so operators can tell
    "page loaded but no code yet" from "page never
    loaded at all".
    """
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    log_extract(
        first_attempt,
        "pattern not found in page content (%d chars) for %s",
        len(text), url_snippet,
    )
    return None


async def cdp_eval_inner_text(
    ws_url: str,
    url_snippet: str,
    first_attempt: bool,
    expression: str = INNER_TEXT_EXPRESSION,
) -> str | None:
    """Send ``Runtime.evaluate(expression)`` and parse; default = innerText.

    Returns the evaluated string on success. Returns
    ``None`` (and logs at the appropriate level) when:

    * The websocket recv times out after 5 s.
    * The CDP response has no ``result.result.value`` chain.

    Raises on connection / serialisation errors — the
    caller wraps in a broad except to demote those to
    DEBUG since they're routine during browser teardown.
    """
    import json as _json

    import websockets as _websockets

    async with _websockets.connect(ws_url, ping_interval=None) as ws:
        await ws.send(_json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
            },
        }))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
        except TimeoutError:
            log_extract(
                first_attempt,
                "content extract timeout for %s",
                url_snippet,
            )
            return None
    data = _json.loads(raw)
    # CDP response shape:
    #  { "id": 1, "result": { "result": { "value": "..." } } }
    value = (
        data.get("result", {})
        .get("result", {})
        .get("value", "")
    )
    if not value:
        log_extract(
            first_attempt,
            "empty page content for %s",
            url_snippet,
        )
        return None
    return str(value)


async def extract_code_from_page(
    target: dict[str, Any],
    pattern: str,
    *,
    first_attempt: bool = False,
    expression: str = INNER_TEXT_EXPRESSION,
) -> str | None:
    """Connect to a CDP target and regex page body for an auth code.

    Used as a fallback when URL-param extraction fails
    (e.g. Epic embeds ``authorizationCode`` in a JSON blob
    inside the intermediate ``/id/api/redirect`` page body).

    Connects via CDP websocket, sends ``Runtime.evaluate``
    with ``document.body.innerText``, and applies
    ``pattern`` to the returned text.

    Args:
        target: CDP target dict with ``webSocketDebuggerUrl``.
        pattern: regex whose first capture group is the
            authorization code.
        first_attempt: if True, log failure reasons at INFO
            so operators can diagnose extraction issues.
    """
    ws_url = target.get("webSocketDebuggerUrl")
    url_snippet = target.get("url", "")[:80]
    if not ws_url:
        log_extract(
            first_attempt,
            "content extract skipped: no webSocketDebuggerUrl for %s",
            url_snippet,
        )
        return None
    try:
        text = await cdp_eval_inner_text(
            ws_url, url_snippet, first_attempt, expression,
        )
    except Exception as exc:
        log_extract(
            first_attempt,
            "content extract failed for %s: %s",
            url_snippet, exc,
        )
        return None
    if text is None:
        return None
    return match_pattern_in_text(text, pattern, url_snippet, first_attempt)


async def try_epic_content_capture(
    target: dict[str, Any],
    url: str,
    state: dict[str, Any],
    start: float,
) -> AuthCaptureResult | None:
    """Run the Epic-specific JSON-blob extraction on a target.

    Updates ``state["content_extract_first_attempt"]``
    so log verbosity is correctly escalated only on
    the first attempt per URL. Returns the capture
    result on success, ``None`` on extraction failure
    (silent — caller retries next poll tick).
    """
    first = url not in state["content_extract_first_attempt"]
    try:
        code = await extract_code_from_page(
            target,
            r'"authorizationCode"\s*:\s*"([^"]+)"',
            first_attempt=first,
        )
    except Exception:
        code = None
    state["content_extract_first_attempt"].add(url)
    if not code:
        return None
    elapsed = time.monotonic() - start
    logger.info(
        "[auth/browser] extracted Epic code from page content after %.1fs",
        elapsed,
    )
    return AuthCaptureResult(
        success=True,
        redirect_url=url,
        params={"code": code},
        elapsed_seconds=elapsed,
    )


async def try_content_fallback(
    targets: list[dict[str, Any]],
    content: ContentCapture | None,
    start: float,
) -> AuthCaptureResult | None:
    """Apply the optional caller-supplied content capture.

    Distinct from ``try_epic_content_capture`` which is
    hardcoded for Epic's URL + regex. This one is the
    generic mechanism — used by stores that pass their
    own ``ContentCapture`` to ``wait_for_redirect``.
    """
    if content is None:
        return None
    for target in targets:
        if content.trigger_url not in target.get("url", ""):
            continue
        try:
            code = await extract_code_from_page(
                target, content.regex, expression=content.expression,
            )
        except Exception as e:
            logger.debug(
                "[auth/browser] content extract failed for %s: %s",
                target.get("url", "")[:80], e,
            )
            continue
        if not code:
            continue
        elapsed = time.monotonic() - start
        logger.info(
            "[auth/browser] extracted code from page content after %.1fs",
            elapsed,
        )
        return AuthCaptureResult(
            success=True,
            redirect_url=target.get("url"),
            params={"code": code},
            elapsed_seconds=elapsed,
        )
    return None
