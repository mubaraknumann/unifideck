"""TLS / SSL helpers — pre-built ``SSLContext`` factories.

Two factory functions for ``ssl.SSLContext``:

* ``ssl_ctx_strict``     — full hostname + certificate
  verification, used everywhere by default;
* ``ssl_ctx_permissive`` — skips hostname check, used only
  for the few stores that ship self-signed certs (CDP'd
  Microsoft login pages, certain Ubisoft endpoints).

Both factories return a fresh ``SSLContext`` per call so
callers can mutate it without affecting siblings.
"""

from .ssl_helpers import ssl_ctx_permissive, ssl_ctx_strict

__all__ = ["ssl_ctx_permissive", "ssl_ctx_strict"]
