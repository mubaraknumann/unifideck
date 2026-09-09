"""One base for every store's typed achievements failure.

``EpicAchievementsError`` and ``GOGAchievementsError`` were separate
``Exception`` subclasses with byte-identical constructors, and the RPC layer
already caught them as a tuple::

    except (GOGAchievementsError, EpicAchievementsError) as e:
        raise RpcError(e.code, **e.context) from e

A tuple catch is the tell that two names describe one concept. Sharing the
base is a small deduplication and a real guard: the RPC now catches
``StoreAchievementsError``, so a third store's achievements failure is
handled the day it is written. Under the tuple it would have escaped as an
unhandled exception — silently, and only for the new store, which is the
kind of gap that reaches a user before it reaches a test (audit register
item 47).

The per-store subclasses are kept rather than collapsed into this one. They
carry no behaviour, but they let a caller narrow to a single store when it
genuinely needs to, and a bare ``raise StoreAchievementsError`` in store code
would lose the name that says which backend failed.
"""
from __future__ import annotations

from typing import Any


class StoreAchievementsError(Exception):
    """A typed achievements failure the RPC layer maps to an ``RpcError``.

    Args:
        code: the stable, translatable reason — ``offline``,
            ``auth_expired``, ``no_client_id``, ``not_authed`` and the
            store-specific ones. This becomes the ``RpcError`` code, so it
            is a wire contract, not a log string.
        context: extra fields for the error envelope, forwarded as
            ``RpcError(code, **context)``.
    """

    def __init__(self, code: str, **context: Any) -> None:
        super().__init__(code)
        self.code = code
        self.context = context
