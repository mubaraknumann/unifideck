"""stores/microsoft/xodus/errors.py — why an Xbox PC install failed, as one code.

Every failure leaves as ``InstallResult.error``: a code, then ``": "`` and a
redacted tail when there is one. ``src/lib/download-errors.ts`` matches the
part before the first colon and shows a translated sentence.

The order of :func:`classify_failure` follows how much each signal knows:

1. A credential diagnostic: the sign-in is gone or unreadable.
2. A content-license diagnostic. The last stage other than
   ``content-parsed`` wins, as in Nexus's ``content-license.ts``.
3. Xbox Live refusing the sign-in (backend 0.1.3+).
4. The stall watchdog.
5. Otherwise ``xodus_exit_<rc>: <tail>``. The frontend's text rules then
   read the CLI's own words (disk space, network).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .diagnostics import Diagnostics, LicenseDiagnostic

if TYPE_CHECKING:
    from .streaming import StreamOutcome

#: Credential error codes that mean "sign in again". Every other code
#: means the stored sign-in could not be read at all.
_SIGN_IN_AGAIN = frozenset({"keyring-missing-data", "keyring-invalid-data"})

_LICENSE_HTTP = {401: "xbox_signin_required", 403: "xbox_license_denied",
                 429: "xbox_license_rate_limited"}
_LICENSE_NETWORK = frozenset({"content-timeout", "content-request", "content-read"})
#: Backend 0.1.3 prints Xbox Live's reply. A 4xx other than 429 is a refusal.
REFUSED_HTTP = re.compile(r"XODUS_XBOX_REFUSED (?:user\.auth|xsts) HTTP 4(?!29)\d\d")


class InstallError(Exception):
    """An install step refused or failed. ``str()`` is the result's error."""

    def __init__(self, code: str, detail: str = "") -> None:
        """``code`` is the stable token; ``detail`` is a redacted tail."""
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def credential_failure(diags: Diagnostics) -> str | None:
    """The code for the last credential error, or None."""
    codes = diags.credential_errors()
    if not codes:
        return None
    if codes[-1] in _SIGN_IN_AGAIN:
        return "xbox_signin_required"
    return "xbox_credentials_unavailable"


def _license_code(diag: LicenseDiagnostic) -> str:
    """The code for one failed content-license stage."""
    if diag.stage == "content-http":
        if diag.status in _LICENSE_HTTP:
            return _LICENSE_HTTP[diag.status]
        if diag.status >= 500:
            return "xbox_license_unavailable"
        return "xbox_license_invalid"
    if diag.stage in _LICENSE_NETWORK:
        return "xbox_license_unavailable"
    if diag.stage == "content-not-entitled":
        return "xbox_license_denied"
    if diag.stage == "content-device-limit":
        # Backend 0.1.5+: Microsoft's licensing reason 501 (ISSUES.md I-032).
        return "xbox_license_device_limit"
    return "xbox_license_invalid"


def license_failure(diags: Diagnostics) -> str | None:
    """The code for the last failed content-license stage, or None.

    A later ``content-parsed`` clears an earlier failure: the CLI retried
    and the license arrived.
    """
    last: LicenseDiagnostic | None = None
    for diag in diags.licenses():
        last = None if diag.stage == "content-parsed" else diag
    return _license_code(last) if last is not None else None


def classify_failure(outcome: StreamOutcome) -> InstallError:
    """The failure that best explains a streaming run that did not succeed."""
    code = credential_failure(outcome.diagnostics) or license_failure(outcome.diagnostics)
    if code is not None:
        return InstallError(code, outcome.tail)
    if REFUSED_HTTP.search(outcome.tail):
        # The CLI's own sentence says "Check your connection", which the
        # frontend would show as a network error (I-062).
        return InstallError("xbox_refused", outcome.tail)
    if outcome.stalled:
        # The tail reads ``stalled: no output for 300s while ... | <tail>``.
        _code, _sep, detail = outcome.tail.partition(": ")
        return InstallError("stalled", detail)
    return InstallError(f"xodus_exit_{outcome.rc}", outcome.tail)


def cli_error_text(err: BaseException) -> str:
    """``str()`` of an ``XodusCliError`` (``xodus_<code>: <tail>``).

    Anything else is an unexpected failure of our own and is reported as
    ``xodus_failed`` with its type, never with its message, which could
    hold a path or a token.
    """
    if hasattr(err, "code"):
        return str(err)
    return f"xodus_failed: {type(err).__name__}"
