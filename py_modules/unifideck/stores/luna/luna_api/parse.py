"""Pure parser for the Luna ``getPage`` library response.

No I/O. The response is a page of widgets; the account's playable games are the
``GAME_TILE`` children of one ``GRID`` widget. Each tile carries its product id,
its title and the page path that opens the game (``/game/<slug>/<ASIN>``, or
just ``/game/<slug>`` for a few titles). ``presentationData`` is a JSON *string*
inside the tile.

A response without any grid is a shape change, not an empty library, so it
raises :class:`LunaApiShapeError` instead of returning ``[]`` (which the sync
layer would take as "remove every Luna entry").
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from unifideck.stores.luna.luna_api.errors import LunaApiShapeError
from unifideck.stores.luna.luna_catalog.models import LunaTitle

logger = logging.getLogger(__name__)

#: ``id`` of the grid that holds the "my games" tiles.
_LIBRARY_GRID_ID = "collection_browse_my_games"
_GRID = "GRID"
_TILE = "GAME_TILE"
_TILE_ID_PREFIX = "game_tile_"
_GAME_PATH_PREFIX = "/game/"


@dataclass(frozen=True)
class ParsedLibrary:
    """The titles of one page plus how many tiles were unusable."""

    titles: list[LunaTitle]
    malformed: int


def parse_library_page(payload: object, luna_host: str) -> ParsedLibrary:
    """Parse a ``getPage`` response into titles.

    ``luna_host`` is the site URL (``https://luna.amazon.de``) the tile paths
    are joined to. Raises :class:`LunaApiShapeError` when no tile grid exists.
    """
    grid = _find_grid(payload)
    base = luna_host.rstrip("/")
    titles: list[LunaTitle] = []
    malformed = 0
    for widget in grid.get("widgets") or []:
        if not isinstance(widget, dict) or widget.get("type") != _TILE:
            continue
        title = _parse_tile(widget, base)
        if title is None:
            malformed += 1
        else:
            titles.append(title)
    _warn_unexpected(grid)
    return ParsedLibrary(titles=titles, malformed=malformed)


def _find_grid(payload: object) -> dict[str, Any]:
    """The library grid widget, by id first, then the first tile grid."""
    widgets = _top_widgets(payload)
    for widget in widgets:
        if widget.get("id") == _LIBRARY_GRID_ID:
            return widget
    for widget in widgets:
        if widget.get("type") == _GRID and _has_tiles(widget):
            return widget
    msg = "Luna getPage response has no game grid"
    raise LunaApiShapeError(msg)


def _top_widgets(payload: object) -> list[dict[str, Any]]:
    """The page's main-content widgets, or a shape error."""
    try:
        widgets = payload["pageMemberGroups"]["mainContent"]["widgets"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        msg = "Luna getPage response has no mainContent widgets"
        raise LunaApiShapeError(msg) from exc
    if not isinstance(widgets, list):
        msg = "Luna getPage mainContent widgets is not a list"
        raise LunaApiShapeError(msg)
    return [w for w in widgets if isinstance(w, dict)]


def _has_tiles(widget: dict[str, Any]) -> bool:
    children = widget.get("widgets")
    return isinstance(children, list) and any(
        isinstance(c, dict) and c.get("type") == _TILE for c in children
    )


def _parse_tile(tile: dict[str, Any], base: str) -> LunaTitle | None:
    """One tile as a :class:`LunaTitle`, or ``None`` when it is unusable."""
    data = _presentation_data(tile)
    raw_id = data.get("gameId") or str(tile.get("id") or "").removeprefix(_TILE_ID_PREFIX)
    name = data.get("title")
    path = _target_path(tile)
    if not (isinstance(raw_id, str) and raw_id and isinstance(name, str) and name and path):
        return None
    return LunaTitle(
        luna_product_id=raw_id,
        name=name,
        browser_url=f"{base}{path}",
        asin=_asin(path, data),
        cover_portrait=_https_url(data.get("imagePortrait")),
        cover_landscape=_https_url(data.get("imageLandscape")),
    )


def _https_url(value: object) -> str | None:
    """``value`` when it is an https URL, else ``None``."""
    return value if isinstance(value, str) and value.startswith("https://") else None


def _presentation_data(tile: dict[str, Any]) -> dict[str, Any]:
    """The tile's ``presentationData`` JSON string as a dict (``{}`` if bad)."""
    raw = tile.get("presentationData")
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _target_path(tile: dict[str, Any]) -> str | None:
    """The ``/game/...`` path of the tile's first action, or ``None``."""
    actions = tile.get("actions")
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
        return None
    target = actions[0].get("target")
    if isinstance(target, str) and target.startswith(_GAME_PATH_PREFIX):
        return target
    return None


def _asin(path: str, data: dict[str, Any]) -> str | None:
    """The ASIN: third path segment, else the ``asin`` of the product URL."""
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[2]:  # /game/<slug>/<ASIN>
        return parts[2]
    hover = data.get("hoverDetails")
    url = hover.get("productUrl") if isinstance(hover, dict) else None
    if isinstance(url, str):
        values = parse_qs(urlparse(url).query).get("asin")
        if values and values[0]:
            return values[0]
    return None


def _warn_unexpected(grid: dict[str, Any]) -> None:
    """Log (without values) a grid carrying anything but game tiles."""
    children = [c for c in grid.get("widgets") or [] if isinstance(c, dict)]
    other = sum(1 for c in children if c.get("type") != _TILE)
    if other or grid.get("actions"):
        logger.warning(
            "[LunaApi] grid has %d non-tile widget(s) and %d action(s); "
            "pagination is not implemented",
            other, len(grid.get("actions") or []),
        )
