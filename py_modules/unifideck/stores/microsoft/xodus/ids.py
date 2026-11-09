"""stores/microsoft/xodus/ids.py — the two ids of an Xbox PC title.

* ``game_id`` is the row's ``store_game_id``: what the frontend, the queue,
  ``games.map`` (``microsoft:<game_id>``), the marker and the events use. It
  may differ from the product id in case, or be the xCloud id of a merged row.
* ``pid`` is the Microsoft Store product id (``^[A-Z0-9]{12}$``) of the
  plan root: what Xodus streams, what the install folder is named after
  (``<base>/Xbox/<PID>``) and what the record file is named after.

The install writes both into the record (``productId``, ``storeGameId``),
so update and uninstall find the record from the ``game_id`` alone, with
no library lookup. That keeps uninstall working with the feature off.
"""
from __future__ import annotations

from typing import Any

from unifideck.launcher.xbox_pc.layout import PID_RE
from unifideck.launcher.xbox_pc.record_lookup import find_install_record


def as_pid(value: Any) -> str | None:
    """``value`` upper-cased when it is a product id, else None."""
    if not isinstance(value, str):
        return None
    upper = value.upper()
    return upper if PID_RE.match(upper) else None


def product_id_for(game_id: str, info: dict[str, Any] | None) -> str | None:
    """The product id to install for ``game_id``, from the library's facts."""
    return as_pid((info or {}).get("product_id")) or as_pid(game_id)


def record_for_game(game_id: str) -> dict[str, Any] | None:
    """The install record of ``game_id``, or None. Blocking.

    The launcher's lookup, so the two can never drift: the record named
    after the id first, then any record whose ``storeGameId`` is ``game_id``.
    """
    return find_install_record(game_id)


def store_game_id(record: dict[str, Any]) -> str:
    """The ``game_id`` a record was installed under (its product id if unset)."""
    value = record.get("storeGameId")
    return value if isinstance(value, str) and value else str(record["productId"])
