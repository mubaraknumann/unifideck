"""launcher/xbox_pc/record_lookup.py — find a game's install record from its game id.

A shortcut launches ``microsoft:<game_id>``, where ``game_id`` is the
library row's ``store_game_id``. That is not always the product id the
record file is named after: it can differ in case, or be the xCloud id of
a merged row. The installer writes both into the record (``productId``
and ``storeGameId``), so the record is found without the library:

1. the record named after ``game_id`` upper-cased, when that is a product id;
2. else the record whose ``storeGameId`` is ``game_id``.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

from typing import Any

from .install_records import list_install_records, read_install_record
from .layout import PID_RE


def find_install_record(game_id: str) -> dict[str, Any] | None:
    """The install record of the title launched as ``game_id``, or None. Blocking."""
    if not isinstance(game_id, str) or not game_id:
        return None
    named = game_id.upper()
    record = read_install_record(named) if PID_RE.match(named) else None
    if record is not None:
        return record
    records, _unreadable = list_install_records()
    for candidate in records:
        if candidate.get("storeGameId") == game_id:
            return candidate
    return None
