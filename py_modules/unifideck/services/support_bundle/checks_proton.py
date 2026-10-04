"""support_bundle/checks_proton.py — Is the plugin-managed GE-Proton usable?

Split out of ``checks.py`` (which sits near its 550-line cap). Answers the
question behind "every game crashes back to the library and the Ubisoft
login won't open": is the GE-Proton every launch defaults to actually
complete? A copy missing its ``toolmanifest.vdf`` makes umu die with
``KeyError: 'manifest'`` before anything starts, and nothing else in the
bundle names it.
"""
from __future__ import annotations

from .check_kit import View as _View
from .check_kit import fail as _fail
from .check_kit import na as _na
from .check_kit import ok as _ok
from .check_kit import warn as _warn
from .spec import CheckResult


def check_managed_ge(view: _View) -> CheckResult:
    """Verdict on the cached latest GE-Proton, from ``probe_proton``."""
    name = "managed_ge_complete"
    block = view.block("managed_ge")
    if not block or "error" in block:
        return _na(name, f"not probed: {block.get('error', 'no data')}")
    tag = block.get("tag")
    if not tag:
        return _na(name, "no managed GE-Proton recorded (proton_ge_latest.json absent)")
    dirs = block.get("dirs") or []
    if not dirs:
        return _warn(
            name,
            f"{tag} is recorded but not on disk - the plugin downloads it on "
            "its next start; until then launches fall back to Proton Experimental",
        )
    usable = [d["path"] for d in dirs if not d.get("problem")]
    if usable:
        return _ok(name, f"{tag} complete at {usable[0]}")
    broken = "; ".join(f"{d['path']}: {d['problem']}" for d in dirs)
    return _fail(
        name,
        f"{tag} is unusable ({broken}) - every launch without a Proton "
        "override fails until it is re-downloaded",
    )
