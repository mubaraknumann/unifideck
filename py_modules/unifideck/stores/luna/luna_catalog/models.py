"""Luna catalog data model."""
from __future__ import annotations

from dataclasses import dataclass

#: The store tag every Luna library entry additionally carries (Req 4.5),
#: alongside ``GameTag.BROWSER``. Drives the frontend's Luna Play control and
#: the per-store reconcile.
LUNA_STORE_TAG = "luna"


@dataclass(frozen=True)
class LunaTitle:
    """One title of the account's Luna library.

    ``luna_product_id`` (``amzn1.luna.product.<UUID>`` or
    ``amzn1.adg.product.<UUID>``) is the stable id and becomes the library
    entry's ``store_game_id``; a title without it is malformed (Req 4.7).
    ``browser_url`` is the game's own page on the Luna site
    (``https://luna.amazon.de/game/<slug>/<ASIN>``), which the launcher opens.
    ``asin`` is ``None`` for the few titles whose page path carries none.
    """

    luna_product_id: str
    name: str
    browser_url: str
    asin: str | None = None
