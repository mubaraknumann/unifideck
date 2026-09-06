"""Class-level RPC method auto-wrapper.

``auto_wrap_rpc_methods`` is the class decorator applied to
every handler class so its public coroutine methods are
automatically wrapped with ``rpc_wrapper``. Saves the
boilerplate of applying ``@rpc_wrapper`` per method on classes
that may have dozens of handlers.

Walks the class's MRO so methods inherited from base classes
are wrapped too, while a duplicate-name guard prevents
double-wrapping when an override shadows a base method.
"""

from __future__ import annotations

import inspect
from typing import Any

from .wrapper import rpc_wrapper


def auto_wrap_rpc_methods(cls: type[Any]) -> type[Any]:
    """Wrap every public async method on ``cls`` (incl. inherited) with rpc_wrapper.

    Iteration order: the full MRO so methods defined on
    parents are included. ``object`` is skipped (no relevant
    methods, and ``object.__init_subclass__`` etc. shouldn't
    be touched).

    Per-method gating:

    * **Leading-underscore names** — skipped (private, not
      RPC-exposed).
    * **Already seen** in a more-derived class — skipped (the
      derived override wins, prevents re-wrapping the base
      version on the same class).
    * **Non-coroutine** — skipped (``rpc_wrapper`` itself
      would raise TypeError on sync functions, but we'd
      rather silently skip non-RPC helpers than crash).

    Mutates ``cls`` in place (the returned value is the same
    class) so it can be used as a regular decorator.

    Args:
        cls: the handler class to mutate.

    Returns:
        The same ``cls``, with every applicable method
        replaced by its wrapped version.
    """
    already_wrapped: set[str] = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, attr in list(klass.__dict__.items()):
            if name.startswith("_") or name in already_wrapped:
                continue
            if not inspect.iscoroutinefunction(attr):
                continue
            setattr(cls, name, rpc_wrapper(attr))
            already_wrapped.add(name)
    return cls
