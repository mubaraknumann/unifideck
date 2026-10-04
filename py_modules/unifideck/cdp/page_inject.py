from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)
async def list_page_targets(
    port: int,
    *,
    timeout: float = 3.0,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
) -> list[dict[str, Any]]:
    """List page targets."""
    url = f"http://127.0.0.1:{port}/json"
    async with aiohttp.ClientSession() as session, session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]
def _target_url_matches(
    target: dict[str, Any], patterns: list[str],
) -> bool:
    """Target URL matches."""
    url = str(target.get("url", ""))
    return any(pattern and pattern in url for pattern in patterns)
_CLOSE_MSG_TYPES = (
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.ERROR,
)
async def _drain_until_reply(
    websocket: aiohttp.ClientWebSocketResponse,
    msg_id: int,
    ws_timeout: float,
    logger_prefix: str,
) -> dict[str, Any] | None:
    """The reply to *msg_id*; None on a closed socket or a CDP-level error."""
    while True:
        message = await websocket.receive(timeout=ws_timeout)
        if message.type in _CLOSE_MSG_TYPES:
            logger.debug(
                "[%s] websocket closed during inject",
                logger_prefix,
            )
            return None
        if message.type != aiohttp.WSMsgType.TEXT:
            continue
        payload = json.loads(message.data)
        if payload.get("id") != msg_id:
            continue
        if "error" in payload:
            logger.debug(
                "[%s] Runtime.evaluate error: %s",
                logger_prefix, payload["error"],
            )
            return None
        return payload if isinstance(payload, dict) else {}

async def inject_into_target(
    target: dict[str, Any],
    sources: list[str],
    *,
    ws_timeout: float,
    logger_prefix: str,
) -> bool:
    """Evaluate each of *sources* in one already-chosen page target.

    One short-lived websocket per call. Callers that match by URL substring
    and poll should use :func:`inject_scripts`; a caller that needs what the
    script returned uses :func:`evaluate_in_target`. Returns False on a
    closed socket, a CDP-level error or a network failure. A script that
    throws inside the page still returns True, because CDP reports that as
    ``exceptionDetails``, not ``error``.
    """
    return await _evaluate_sources(target, sources, ws_timeout, logger_prefix) is not None


async def evaluate_in_target(
    target: dict[str, Any],
    source: str,
    *,
    ws_timeout: float,
    logger_prefix: str,
) -> tuple[bool, Any]:
    """Evaluate *source* in one page target; ``(ok, returned value)``.

    ``ok`` fails exactly as :func:`inject_into_target` does. The value is
    None when the script threw or returned nothing serialisable.
    """
    values = await _evaluate_sources(target, [source], ws_timeout, logger_prefix)
    if values is None:
        return False, None
    return True, values[0] if values else None


def _returned_value(reply: dict[str, Any]) -> Any:
    """``result.result.value`` of a ``Runtime.evaluate`` reply, else None."""
    outer = reply.get("result")
    inner = outer.get("result") if isinstance(outer, dict) else None
    return inner.get("value") if isinstance(inner, dict) else None


async def _evaluate_sources(
    target: dict[str, Any],
    sources: list[str],
    ws_timeout: float,
    logger_prefix: str,
) -> list[Any] | None:
    """Each non-empty source's returned value, in order; None on failure."""
    ws_url = target.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str) or not ws_url:
        return None
    msg_id = 0
    values: list[Any] = []
    try:
        async with aiohttp.ClientSession() as session, session.ws_connect(  # type: ignore[call-overload]
            ws_url,
            heartbeat=10,
            autoping=True,
            # aiohttp 3.10 introduced ClientWSTimeout for ws_connect; older
            # releases accepted ClientTimeout. The runtime accepts both, but
            # mypy uses the latest stub, so the overload doesn't match. Ignore
            # the overload mismatch to keep the call compatible across aiohttp
            # versions.
            timeout=aiohttp.ClientTimeout(total=ws_timeout),
        ) as websocket:
            for source in sources:
                if not source:
                    continue
                msg_id += 1
                await websocket.send_json({
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": source,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "userGesture": True,
                    },
                })
                reply = await _drain_until_reply(
                    websocket, msg_id, ws_timeout, logger_prefix,
                )
                if reply is None:
                    return None
                values.append(_returned_value(reply))
        return values
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        logger.debug(
            "[%s] inject into %s failed: %s",
            logger_prefix, target.get("id"), exc,
        )
        return None

async def inject_scripts(
    port: int,
    sources: list[str],
    *,
    url_patterns: list[str],
    timeout: float = 45.0,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    logger_prefix: str = "cdp-inject",
    poll_delay: float = 0.5,
) -> bool:
    """Inject ``sources`` into every matching CDP page target.

    Polls the CDP target list at ``poll_delay`` intervals until
    ``timeout`` elapses or every matching target has been
    injected successfully. Returns True if at least one
    injection happened ; False if the deadline expired with no
    matches found.

    Refactor history (2026-05-14): the polling loop inlined the
    try/except around ``list_page_targets``, the URL filtering
    comprehension, the empty-targets continue, the
    inject-and-update flags, and the early-success return —
    cyclomatic complexity 13. Pulled the per-iteration work
    into ``_attempt_inject_cycle`` so this function reads as the
    deadline envelope only.
    """
    if not sources or not url_patterns:
        return False

    deadline = asyncio.get_running_loop().time() + timeout
    injected_once = False
    while asyncio.get_running_loop().time() < deadline:
        all_ok, had_success = await _attempt_inject_cycle(
            port, sources, url_patterns, timeout, logger_prefix,
        )
        if had_success:
            injected_once = True
        if injected_once and all_ok:
            return True
        await asyncio.sleep(poll_delay)

    if injected_once:
        return True
    logger.warning(
        "[%s] timed out waiting for matching page (patterns=%r)",
        logger_prefix, url_patterns,
    )
    return False


async def _attempt_inject_cycle(
    port: int,
    sources: list[str],
    url_patterns: list[str],
    timeout: float,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    logger_prefix: str,
) -> tuple[bool, bool]:
    """One poll iteration : list targets, filter, inject, return flags.

    Returns ``(all_ok, had_success)`` :

        * ``all_ok`` = every matching target was injected this
          cycle (or there were no matching targets — vacuously
          true in that case ; the caller distinguishes via
          ``had_success`` not flipping).
        * ``had_success`` = at least one target was injected.

    Returns ``(True, False)`` for both "list_page_targets raised"
    and "no targets matched" so the caller's "all_ok AND
    injected_once" guard still requires real progress before
    returning success.
    """
    try:
        targets = await list_page_targets(port, timeout=3.0)
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        logger.debug(
            "[%s] list_page_targets failed: %s", logger_prefix, exc,
        )
        return True, False

    page_targets = [
        t for t in targets
        if t.get("type") == "page" and _target_url_matches(t, url_patterns)
    ]
    if not page_targets:
        return True, False

    return await _inject_into_matching_targets(
        page_targets, sources, timeout, logger_prefix,
    )
async def _inject_into_matching_targets(
    page_targets: list[dict[str, Any]],
    sources: list[str],
    timeout: float,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    logger_prefix: str,
) -> tuple[bool, bool]:
    """Inject into matching targets."""
    all_ok = True
    had_success = False
    for target in page_targets:
        ok = await inject_into_target(
            target,
            sources,
            ws_timeout=min(15.0, timeout),
            logger_prefix=logger_prefix,
        )
        if ok:
            had_success = True
            logger.info(
                "[%s] injected %d script(s) into %s",
                logger_prefix, len(sources),
                target.get("url", "?"),
            )
        else:
            all_ok = False
    return all_ok, had_success
@contextlib.asynccontextmanager
async def _session_timeout(
    total: float,
) -> AsyncIterator[aiohttp.ClientTimeout]:
    """Async context manager that yields a ``ClientTimeout``."""
    yield aiohttp.ClientTimeout(total=total)
