"""EA ids: the row id Unifideck stores, and the offer id EA uses.

EA offer ids come in two shapes: ``Origin.OFR.50.0002540`` and ``OFB-EAST:59474``.
A colon is not allowed in a game id (``core.types.identifiers``) and is unsafe in
paths built from it (prefixes, colon-separated variables), so the row id spells
the colon as ``__``. No offer id contains ``__``, so the mapping reverses exactly.
"""

from __future__ import annotations

STORE = "ea"

_COLON = ":"
_COLON_IN_ROW = "__"


def row_id(offer_id: str) -> str:
    """The game id Unifideck stores for ``offer_id``."""
    return offer_id.replace(_COLON, _COLON_IN_ROW)


def offer_id(game_id: str) -> str:
    """The EA offer id behind a game id from :func:`row_id`."""
    return game_id.replace(_COLON_IN_ROW, _COLON)
