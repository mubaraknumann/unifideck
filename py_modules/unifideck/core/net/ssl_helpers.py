"""TLS SSLContext factories with thread-safe lazy singletons.

Two singleton ``SSLContext`` instances kept module-level
because ``ssl.create_default_context`` is non-trivial
(reads cert chain from disk) and reusing the context
across requests is the standard ``ssl`` recommendation.

The double-check locking pattern (check, lock, check)
keeps the first-call cost paid exactly once even under
concurrent first-touch from multiple tasks.

The permissive context emits a one-shot WARN log on
first use so operators see a clear signal in plugin logs
that some endpoint is bypassing strict TLS — with the
caller-supplied reason for traceability.
"""

from __future__ import annotations

import logging
import ssl
from threading import Lock

logger = logging.getLogger(__name__)

_strict_lock = Lock()
_strict_ctx: ssl.SSLContext | None = None
_permissive_lock = Lock()
_permissive_ctx: ssl.SSLContext | None = None
_permissive_warned = False

def ssl_ctx_strict() -> ssl.SSLContext:
    """Return the shared strict-mode SSLContext (singleton).

    Enforces hostname check + full cert chain verification against the
    system trust store **plus** the vendored ``certifi`` bundle. Adding
    certifi's roots (never replacing the system's) answers the "outdated
    Deck cert store" problem that ``ssl_ctx_permissive`` was reached for:
    a stale OS store still verifies against a current Mozilla root set,
    and a host the OS trusts keeps working. Should be the first choice
    for every endpoint, and the only one for anything carrying a
    credential.

    Returns:
        The cached strict context. Subsequent calls are
        O(1) dict lookups.
    """
    global _strict_ctx
    if _strict_ctx is None:
        with _strict_lock:
            if _strict_ctx is None:
                ctx = ssl.create_default_context()
                _add_certifi_roots(ctx)
                _strict_ctx = ctx
    return _strict_ctx


def _add_certifi_roots(ctx: ssl.SSLContext) -> None:
    """Load the vendored certifi bundle into ``ctx``, if it is present."""
    try:
        import certifi  # vendored in py_modules/

        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError) as exc:
        logger.warning(
            "[ssl_helpers] certifi bundle unavailable (%s); verifying "
            "against the system trust store only", exc,
        )

def ssl_ctx_permissive(reason: str) -> ssl.SSLContext:
    """Return the permissive SSLContext (hostname + cert checks disabled).

    Never for an endpoint that sends or returns a credential
    (OAuth, Xbox Live / XSTS, store sessions): an attacker on
    the same network can impersonate any host to it. Every
    Microsoft endpoint uses :func:`ssl_ctx_strict`, and a test
    keeps it that way (``test_microsoft_strict_tls``). The first call logs at
    WARN with the caller-supplied ``reason`` so operators
    can audit why permissive mode was needed.

    The warn is one-shot per process to keep logs clean
    even when the permissive context is hit thousands of
    times per session.

    Args:
        reason: short description of why permissive is
            needed (typically a store name + brief
            justification).

    Returns:
        The cached permissive context.
    """
    global _permissive_ctx, _permissive_warned
    if _permissive_ctx is None:
        with _permissive_lock:
            if _permissive_ctx is None:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                _permissive_ctx = ctx
    if not _permissive_warned:
        logger.warning(
            "[ssl_helpers] permissive SSL context requested — "
            "hostname + cert chain validation DISABLED. Reason: %s",
            reason,
        )
        _permissive_warned = True
    return _permissive_ctx
