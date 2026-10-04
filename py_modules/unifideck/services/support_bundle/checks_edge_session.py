"""support_bundle/checks_edge_session.py — Will the Xbox kiosk ask to sign in?

Companion to ``probe_edge_session``, which collects the state; this turns it
into one line at the top of ``diagnostics.txt``. Split out of ``checks.py``
because that module is near its size cap.

The order is the order the failure happens in: an Edge that cannot write the
profile loses every sign-in whatever the user picks, so it is reported before
a sign-in that was only kept for the session.
"""
from __future__ import annotations

from typing import Any

from .check_kit import View, fail, na, ok, warn
from .spec import CheckResult

_NAME = "xbox_signin_saved"


def check_edge_session(view: View) -> CheckResult:
    """Verdict on whether the Edge profile kept the Microsoft sign-in."""
    block = view.block("edge_session")
    if not block or "error" in block:
        return na(_NAME, "Edge profile state not collected")
    if not block.get("profile_exists"):
        return na(_NAME, "no Edge profile yet (Xbox Cloud Gaming never opened)")
    browser = block.get("browser") or {}
    if browser.get("profile_writable") is False:
        return fail(_NAME, (
            f"the {browser.get('scope', '')} Edge Flatpak has no write access to "
            f"{block.get('profile_dir')} - every Microsoft sign-in is lost when "
            "Edge closes. Restore its home access (Flatseal or flatpak override)"
        ) + _aborts(block))
    return _session_verdict(block)


def _session_verdict(block: dict[str, Any]) -> CheckResult:
    """Persistent, session-only or missing Microsoft sign-in cookies."""
    state = block.get("microsoft_session")
    if state == "persistent":
        return ok(_NAME, "Microsoft sign-in saved in the Edge profile" + _aborts(block))
    if state == "session_only":
        return warn(_NAME, (
            "Microsoft sign-in is kept only for the session ('Stay signed in' "
            "declined?) - the Xbox kiosk asks to sign in after every launch"
        ) + _aborts(block))
    if _visited_xbox(block):
        return warn(_NAME, (
            "xbox.com was opened but no Microsoft sign-in is saved in the Edge "
            "profile - the Xbox kiosk will ask to sign in"
        ) + _aborts(block))
    return na(_NAME, "no Microsoft sign-in in the Edge profile")


def _visited_xbox(block: dict[str, Any]) -> bool:
    """True when the profile holds any xbox.com cookie."""
    return any("xbox.com" in str(c.get("host", "")) for c in block.get("cookies") or [])


def _aborts(block: dict[str, Any]) -> str:
    """A suffix naming Edge aborts, which skip writing recent cookies."""
    count = (block.get("log") or {}).get("fatal_count") or 0
    if not count:
        return ""
    return f"; Edge aborted {count} time(s) in edge-auth.log (recent cookies are not written on abort)"
