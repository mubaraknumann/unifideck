"""TLS / SSL helpers — pre-built ``SSLContext`` factories.

Two factory functions for ``ssl.SSLContext``:

* ``ssl_ctx_strict``     — full hostname + certificate
  verification against the system store plus the vendored
  certifi bundle, used everywhere by default;
* ``ssl_ctx_permissive`` — hostname + certificate checks
  disabled. Never for an endpoint that carries a credential.

Both factories return a fresh ``SSLContext`` per call so
callers can mutate it without affecting siblings.
"""

from .ssl_helpers import ssl_ctx_permissive, ssl_ctx_strict

__all__ = ["ssl_ctx_permissive", "ssl_ctx_strict"]
