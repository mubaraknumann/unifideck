"""Structured RPC error type — code + free-form context dict.

``RpcError`` is the canonical exception type for everything that
can go wrong inside an RPC handler. Unlike a bare ``Exception``
with a string message, it carries:

* a stable ``code`` string (e.g. ``"service_unavailable"``,
  ``"invalid_uri"``) the frontend can switch on for typed
  error handling and i18n;
* an arbitrary ``context`` dict with structured fields that
  feed into the error message and frontend toast (store id,
  game id, verb name, etc.).

Caught by the ``rpc_wrapper`` decorator which
serialises the error into the RPC response payload.
"""

from __future__ import annotations

from typing import Any


class RpcError(Exception):
    """Typed exception for RPC layer failures.

    Attributes:
        code: stable error identifier (the same value across
            plugin versions for the same condition). Used by
            the frontend for typed error handling.
        context: free-form dict of structured fields surfaced
            in the error message and frontend toast. Always a
            dict copy (never the caller's reference).
    """

    def __init__(self, code: str, message: str = "", **context: Any) -> None:
        """Build an error with a code, optional message, and kwarg context.

        Falls back to using ``code`` as the exception's
        ``__str__`` when no explicit message is given — keeps
        log lines informative without forcing every callsite
        to repeat the code as a message.

        Args:
            code: stable error identifier.
            message: optional human-readable message; defaults
                to ``code`` when omitted.
            **context: any structured fields to attach.
        """
        super().__init__(message or code)
        self.code = code
        self.context: dict[str, Any] = dict(context)
