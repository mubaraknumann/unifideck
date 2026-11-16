"""Shared Luna CDP helpers (one owner for the page-target-attach step).

The detail-page play trigger (:mod:`luna_play_activator`) and the
platform-probe verify (:mod:`luna_platform_spoofer`) both begin the same way:
ask the browser for its targets, find the first ``page`` target, and attach to
it in flatten mode to get a CDP session id. The bodies were byte-for-byte the
same CDP calls in the same order, differing only in the exception each raises
on failure — which is exactly the body-shape duplication
``validate_architecture.py`` check 13 flags.

Promoted here (SHARED_HELPERS row ``attach_to_page_target``) so there is one
definition. Each caller keeps its own failure semantics by passing a
``raise_no_target`` / ``raise_no_session`` factory that builds the module's own
exception with the message that caller already surfaced.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


class _Sendable(Protocol):
    """The one CDP seam this helper needs: a ``send`` coroutine."""

    async def send(
        self, method: str, params: dict[str, Any] | None = ...,
        *, session_id: str | None = ...,
    ) -> dict[str, Any]:
        """Send a CDP command and return its result mapping."""
        ...


async def attach_to_page_target(
    session: Any,
    *,
    raise_no_target: Callable[[], Exception],
    raise_no_session: Callable[[], Exception],
) -> str:
    """Return a CDP session id attached to the active Luna page target.

    Asks the browser for its targets and attaches (flatten mode) to the first
    ``page`` target — the loaded Luna page the caller must run against.

    Args:
        session: a browser-level CDP session exposing ``send`` (the
            ``BrowserCdpSession`` protocol / its aiohttp impl / a test mock).
        raise_no_target: builds the exception to raise when no ``page`` target
            exists — a structural/transport failure the caller names itself.
        raise_no_session: builds the exception to raise when the attach returns
            no ``sessionId``.

    Returns:
        The attached target's CDP session id.
    """
    targets_result = await session.send("Target.getTargets")
    targets = targets_result.get("targetInfos") or []
    page_target = next(
        (t for t in targets if t.get("type") == "page"), None,
    )
    if not page_target or not page_target.get("targetId"):
        raise raise_no_target()
    attach_result = await session.send(
        "Target.attachToTarget",
        {"targetId": page_target["targetId"], "flatten": True},
    )
    session_id = attach_result.get("sessionId")
    if not session_id:
        raise raise_no_session()
    return str(session_id)
