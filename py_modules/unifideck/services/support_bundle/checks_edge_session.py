"""support_bundle/checks_edge_session.py — Is the browser ready, and the sign-in saved?

Companion to ``probe_edge_session``, which collects the state; this turns it
into one line at the top of ``diagnostics.txt``. Split out of ``checks.py``
because that module is near its size cap.

The order is the order the failure happens in: without a working browser no
sign-in window, store window or stream opens at all, so that is reported
before the Microsoft cookies. Those matter only when a stream falls back to
xbox.com: since 0.7.7 streams play in Unifideck's own player.
"""
from __future__ import annotations

from typing import Any

from .check_kit import View, fail, na, ok, warn
from .spec import CheckResult

_NAME = "xbox_signin_saved"


def check_edge_session(view: View) -> CheckResult:
    """Verdict on the plugin's browser, then on the saved Microsoft sign-in."""
    block = view.block("edge_session")
    if not block or "error" in block:
        return na(_NAME, "browser profile state not collected")
    browser = block.get("browser") or {}
    if browser.get("kind") == "absent":
        return warn(_NAME, (
            "the plugin's browser is not downloaded yet - the first sign-in, store "
            "window or stream offers to download it (about 200 MB)"
        ))
    if browser.get("executable") is False:
        return fail(_NAME, (
            f"the plugin's browser at {browser.get('path')} cannot run (not "
            "executable) - delete ~/.local/share/unifideck/browser and it is "
            "downloaded again"
        ))
    if not block.get("profile_exists"):
        return na(_NAME, "no browser profile yet (no sign-in window opened)")
    return _session_verdict(block)


def _session_verdict(block: dict[str, Any]) -> CheckResult:
    """Persistent, session-only or missing Microsoft sign-in cookies."""
    state = block.get("microsoft_session")
    if state == "persistent":
        return ok(_NAME, "Microsoft sign-in saved in the browser profile" + _aborts(block))
    if state == "session_only":
        return warn(_NAME, (
            "Microsoft sign-in is kept only for the session ('Stay signed in' "
            "declined?) - a stream that falls back to xbox.com asks to sign in"
        ) + _aborts(block))
    if _visited_xbox(block):
        return warn(_NAME, (
            "xbox.com was opened but no Microsoft sign-in is saved in the browser "
            "profile - a stream that falls back to xbox.com asks to sign in"
        ) + _aborts(block))
    return na(_NAME, "no Microsoft sign-in in the browser profile")


def _visited_xbox(block: dict[str, Any]) -> bool:
    """True when the profile holds any xbox.com cookie."""
    return any("xbox.com" in str(c.get("host", "")) for c in block.get("cookies") or [])


def _aborts(block: dict[str, Any]) -> str:
    """A suffix naming browser aborts, which skip writing recent cookies."""
    count = (block.get("log") or {}).get("fatal_count") or 0
    if not count:
        return ""
    return f"; the browser aborted {count} time(s) in edge-auth.log (recent cookies are not written on abort)"
