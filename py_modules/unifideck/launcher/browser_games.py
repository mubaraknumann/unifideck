"""Browser games: titles played in an Edge window at a URL, never installed.

Two stores have them. Xbox Cloud Gaming titles stream from Microsoft
(``kind="stream"``), and itch.io HTML5-only games run in the page itself
(``kind="web"``). Everything about launching one is the same: no games.map
row, an Edge kiosk window on the URL, and the launcher blocking until that
window closes. This module is the one place that decides whether a game is a
browser game and where it opens.

The decision is per **game**, not per store, because itch.io mixes the two:
most of its games install and a few only run in a browser. A store marks a
browser game by giving it ``GameTag.BROWSER`` and a ``metadata["browser_url"]``;
the launcher, which has no library RPC, reads both back from
``library_cache.json``, the same file it already used to find an install path.

Stdlib only: this runs under the launcher's system Python.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

LIBRARY_CACHE = "~/.local/share/unifideck/library_cache.json"
BROWSER_URL_KEY = "browser_url"
BROWSER_TAG = "browser"

#: The page that *starts* an xCloud stream. ``/play/games/{id}`` is only a
#: store page (Edge opened and the game never started). The one Microsoft
#: constant here, because the launcher must still derive it for a library
#: cache written before ``browser_url`` existed; the Microsoft catalog imports
#: it from here, so it is defined once.
XCLOUD_PLAY_URL = "https://www.xbox.com/play/launch/{game_id}"
_XCLOUD_TAG = "xcloud"

#: Where each kind may open. A browser game's URL comes from store data and
#: the window has no address bar, so anything else is refused rather than
#: shown: a cloud stream only ever starts on xbox.com, and an HTML5 game only
#: ever runs on its itch.io page.
_STREAM_HOSTS = frozenset({"www.xbox.com", "xbox.com"})
_WEB_HOST = "itch.io"


@dataclass(frozen=True)
class BrowserTarget:
    """Where a browser game opens, and which kind it is.

    ``kind`` is ``"stream"`` for a cloud stream that signs in to its service
    (xCloud) and ``"web"`` for a game that runs in the page (itch.io HTML5).
    """

    url: str
    kind: str


def cached_game(store: str, game_id: str) -> dict[str, Any] | None:
    """The game's record from the last sync, or None when absent/unreadable."""
    cache = Path(LIBRARY_CACHE).expanduser()
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    libraries = data.get("libraries") if isinstance(data, dict) else None
    for game in (libraries or {}).get(store) or []:
        if isinstance(game, dict) and game.get("store_game_id") == game_id:
            return game
    return None


def is_allowed_browser_url(url: str, kind: str) -> bool:
    """Whether a browser game of *kind* may open *url*: https, on its own host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    if kind == "stream":
        return host in _STREAM_HOSTS
    return host == _WEB_HOST or host.endswith("." + _WEB_HOST)


def browser_target(store: str, game_id: str) -> BrowserTarget | None:
    """The browser target for a game with no games.map row, or None.

    A Microsoft title in a pre-0.7.6 cache has the ``xcloud`` tag but no
    ``browser_url``, and a Microsoft title missing from the cache entirely
    was still launched as xCloud before this module existed; both keep
    working. Every Microsoft title is a cloud stream (the store installs
    nothing), so that fallback cannot misroute a real install.
    """
    game = cached_game(store, game_id) or {}
    tags = set(game.get("tags") or [])
    url = str((game.get("metadata") or {}).get(BROWSER_URL_KEY) or "")
    streamed = _XCLOUD_TAG in tags or store == "microsoft"
    kind = "stream" if streamed else "web"
    if url and (BROWSER_TAG in tags or streamed):
        if is_allowed_browser_url(url, kind):
            return BrowserTarget(url=url, kind=kind)
        logger.warning(
            "[browser_games] refusing %s %s: %r is not an allowed %s URL",
            store, game_id, url[:120], kind,
        )
    if streamed:
        return BrowserTarget(url=XCLOUD_PLAY_URL.format(game_id=game_id), kind="stream")
    return None
