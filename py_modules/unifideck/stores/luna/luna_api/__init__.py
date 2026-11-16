"""Luna web API: the library catalog without a browser.

* :mod:`.regions` — per-country constants (marketplace id, tempo host, ...).
* :mod:`.parse` — pure parser for the ``getPage`` "my games" response.
* :mod:`.client` — :class:`LunaApiClient`: session cookies -> access token ->
  ``getPage``.
* :mod:`.errors` — the typed errors the store maps to its status values.
"""
from __future__ import annotations

from unifideck.stores.luna.luna_api.client import LunaApiClient
from unifideck.stores.luna.luna_api.errors import (
    LunaApiError,
    LunaApiShapeError,
    LunaSessionError,
)
from unifideck.stores.luna.luna_api.parse import ParsedLibrary, parse_library_page
from unifideck.stores.luna.luna_api.regions import LunaRegion, resolve_region

__all__ = [
    "LunaApiClient",
    "LunaApiError",
    "LunaApiShapeError",
    "LunaRegion",
    "LunaSessionError",
    "ParsedLibrary",
    "parse_library_page",
    "resolve_region",
]
