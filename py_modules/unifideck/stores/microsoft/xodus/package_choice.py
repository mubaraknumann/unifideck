"""stores/microsoft/xodus/package_choice.py — pick the package an install streams.

``xodus-cli resolve-package PID --json`` answers (Balatro, measured)::

    {"schema":1, "rootProductId":"9PK087LNGJC5", "market":"IN",
     "status":"ready",
     "choices":[{"contentId":"1760268b-...","kind":"Game",
                 "packageFormat":"MSIXVC","platform":"Windows.Desktop",
                 "productId":"9PK087LNGJC5","supported":true,"title":"Balatro"},
                {"contentId":"3e7fcb43-...","kind":"Game","packageFormat":"XVC",
                 "platform":"Windows.Xbox","supported":false, ...}]}

Exactly one supported ``Game`` choice is picked on its own. None means the
title has no PC package we can install. More than one means several
editions, and v1 has no picker, so the install is refused rather than
guessing one. The CLI re-checks the chosen content id itself
(``--selected-content-id``).
"""
from __future__ import annotations

from typing import Any

from unifideck.launcher.xbox_pc.argv import CONTENT_ID_RE

from .errors import InstallError

PLAN_SCHEMA = 1
PLAN_STATUSES = frozenset({"ready", "choice", "unavailable"})


def _check_plan(plan: Any, root_pid: str) -> list[dict[str, Any]]:
    """The plan's choices. Raises ``xbox_resolve_failed`` on a bad envelope."""
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise InstallError("xbox_resolve_failed", "unknown plan schema")
    if str(plan.get("rootProductId", "")).upper() != root_pid:
        raise InstallError("xbox_resolve_failed", "plan names another product")
    if plan.get("status") not in PLAN_STATUSES:
        raise InstallError("xbox_resolve_failed", "unknown plan status")
    choices = plan.get("choices")
    if not isinstance(choices, list):
        raise InstallError("xbox_resolve_failed", "plan has no choices")
    return [c for c in choices if isinstance(c, dict)]


def _installable(choice: dict[str, Any]) -> bool:
    """A supported PC game package with a well-formed content id."""
    return (
        choice.get("supported") is True
        and choice.get("kind") == "Game"
        and choice.get("platform") == "Windows.Desktop"
        and isinstance(choice.get("contentId"), str)
        and CONTENT_ID_RE.match(choice["contentId"]) is not None
    )


def installable_content_ids(plan: Any, root_pid: str) -> list[str]:
    """Content ids of every installable ``Game`` choice, deduplicated."""
    seen: list[str] = []
    for choice in _check_plan(plan, root_pid):
        cid = str(choice.get("contentId", "")).lower()
        if _installable(choice) and cid not in seen:
            seen.append(cid)
    return seen


def select_content(plan: Any, root_pid: str, prefer_cid: str | None = None) -> str:
    """The content id to stream for ``root_pid``.

    ``prefer_cid`` (the recorded one, on an update or a resume) wins when
    it is still installable. Raises ``InstallError`` with
    ``xbox_package_unavailable`` or ``xbox_package_choice`` otherwise.
    """
    games = installable_content_ids(plan, root_pid)
    if prefer_cid and prefer_cid.lower() in games:
        return prefer_cid.lower()
    if len(games) == 1:
        return games[0]
    if not games:
        raise InstallError("xbox_package_unavailable")
    raise InstallError("xbox_package_choice", f"{len(games)} editions")
