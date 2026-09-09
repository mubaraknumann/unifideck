"""Store timestamps of unknown flavour, parsed one way.

Every store API answers "when did this happen" in at least two shapes — an
ISO-8601 string or a numeric epoch — and returns nothing at all when the
event has not happened. Three copies of this parse existed (audit register
item 47): ``epic/achievements``, ``epic/sessions`` and ``gog/achievements``,
reading achievement unlock times and OAuth expiries.

Check 13 grouped only the two Epic copies. The GOG one escaped by a single
call: it omitted ``.replace("Z", "+00:00")``. That happens to be harmless on
the interpreter it runs under — the backend is Decky's bundled Python 3.11,
and ``datetime.fromisoformat`` gained native ``Z`` support in 3.11 — but it
is the sort of difference that only stays harmless by accident, and the
launcher's Python can be as old as 3.10. The shared version keeps the
normalisation, so the answer no longer depends on which interpreter is
running.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_timestamp(value: Any) -> float | None:
    """An ISO-8601 string or numeric epoch → epoch float, else ``None``.

    ``None`` covers every "no answer" case a store can give — an absent key,
    an empty string, a null, and a malformed date — because each means the
    same thing to the callers: there is no time to show, and no expiry to
    compare against.

    Note ``0`` and ``""`` return ``None`` rather than ``0.0``. Epoch zero as
    a real unlock time is not a case any store produces, whereas a falsy
    placeholder is one they all do.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None
