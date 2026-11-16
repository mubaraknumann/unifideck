"""Typed errors of the Luna web API client."""
from __future__ import annotations


class LunaApiError(Exception):
    """A Luna API call failed (transport, HTTP status, bad JSON)."""


class LunaApiShapeError(LunaApiError):
    """The response parsed as JSON but no longer has the expected shape."""


class LunaSessionError(LunaApiError):
    """The Amazon session is missing, expired or rejected.

    ``reason`` is a short machine-readable label (``no_cookies``, the token
    endpoint's ``invalidReason``, or ``http_<status>``); never a secret.
    """

    def __init__(self, reason: str) -> None:
        """Carry the short reason label."""
        super().__init__(reason)
        self.reason = reason
