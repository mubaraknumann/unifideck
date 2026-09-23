"""itch.io library: owned keys → ``Game`` rows, with butler's installs overlaid.

The two butlerd reads behave in ways the docs do not warn about (measured on
butler 15.31.0, 2026-09-23):

* **Always ``fresh: true``.** Without it the answer comes from butler's local
  cache, which on a new database is empty, and "empty" arrives as ``[]``,
  not as an error. Returned as-is, that would tell the reconcile the user owns
  nothing and delete every itch.io shortcut (stores.md invariant 2).
* **Owned keys are also what unlock installs.** Uploads of a paid game only
  resolve once ``Fetch.ProfileOwnedKeys`` has stored the download key, so a
  sync must precede the first install on a new database.

What the library holds: everything **owned** (purchased or claimed: the
owned keys), plus the **free** games the user has put in their itch.io
*collections*. Collection games are not owned (a collection is a bookmark
list), but a free game downloads without a key, so it installs exactly like
an owned one. itch.io omits ``minPrice`` for free games (measured: all seven
collection games had none, the owned paid ones 100 and 499), so a collection
game with a ``minPrice`` is a paid game the user has not bought, and is left
out: it could never install. One the user did buy is already an owned key.

Any failure here returns ``None`` ("could not read"), never ``[]``. That includes
a failed collections read, or the collection games' shortcuts would be
deleted on a flaky sync.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.core.types import Game
from unifideck.core.types.events import GameTag
from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.shared.install_status import merge_install_status

from .butlerd import ButlerDaemon, ButlerdError
from .uploads import choose_upload, has_web_build

logger = logging.getLogger(__name__)

STORE = "itch"
#: itch.io hosts tools, soundtracks, books and asset packs too; only games
#: become shortcuts.
_GAME_CLASSIFICATION = "game"
#: Platforms Unifideck can run: natively, or under Proton.
_RUNNABLE_PLATFORMS = ("linux", "windows")
#: itch.io answers bursts with 429; butler retries 503 on its own.
_RATE_LIMIT_STATUSES = (429, 503)
_BACKOFF_S = (2.0, 5.0, 15.0)
_PAGE_LIMIT = 100
#: Parallel upload lookups for untagged games; butler throttles itself to
#: 8 requests/s, so more buys nothing.
_UNTAGGED_CONCURRENCY = 4


def is_game(game: dict[str, Any]) -> bool:
    """A game, not a tool, soundtrack, book or asset pack."""
    return game.get("classification") == _GAME_CLASSIFICATION


def is_runnable_game(game: dict[str, Any]) -> bool:
    """A game whose page is tagged with a Linux or Windows build.

    ``platforms`` values are strings such as ``"all"``, not booleans; a
    missing key means the platform is absent. An untagged game is not
    rejected here: :meth:`ItchLibraryReader._classify_untagged` asks for its
    uploads, because developers often never tick the boxes (DELTARUNE).
    """
    platforms = game.get("platforms") or {}
    return is_game(game) and any(platforms.get(p) for p in _RUNNABLE_PLATFORMS)


def to_game(game: dict[str, Any], *, owned: bool = True, web: bool = False) -> Game:
    """Map a butlerd ``Game`` object to our record.

    A *web* game (HTML5 only) becomes a browser game: ``GameTag.BROWSER`` plus
    its itch.io page as ``browser_url``, which the launcher opens in Edge
    (``launcher/browser_games``). The page is where itch.io runs the game.
    """
    metadata: dict[str, Any] = {
        "url": game.get("url") or "",
        "short_text": game.get("shortText") or "",
        # Last-resort grid art (``services/artwork`` phase 4).
        "cover_url": game.get("coverUrl") or "",
        "still_cover_url": game.get("stillCoverUrl") or "",
        # "owned" = an owned key (bought or claimed); "collection" = a
        # free game the user bookmarked in one of their collections.
        "source": "owned" if owned else "collection",
        "platforms": sorted(
            p for p in _RUNNABLE_PLATFORMS if (game.get("platforms") or {}).get(p)
        ),
    }
    if web and game.get("url"):
        metadata[BROWSER_URL_KEY] = game["url"]
    return Game(
        app_id=0,
        store=STORE,
        store_game_id=str(game["id"]),
        title=str(game.get("title") or f"itch.io game {game['id']}"),
        icon_url=game.get("coverUrl") or game.get("stillCoverUrl") or None,
        tags=[GameTag.BROWSER] if BROWSER_URL_KEY in metadata else [],
        metadata=metadata,
    )


def _cave_entry(cave: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """``(game_id, install record)`` for a cave with a game and a folder."""
    game = cave.get("game") or {}
    folder = (cave.get("installInfo") or {}).get("installFolder")
    if not (game.get("id") and folder):
        return None
    return str(game["id"]), {
        "install_path": folder,
        "cave_id": cave.get("id"),
        "upload": cave.get("upload") or {},
        "title": str(game.get("title") or ""),
    }


class ItchLibraryReader:
    """Reads the library and install records through the daemon."""

    def __init__(self, daemon: ButlerDaemon) -> None:
        self._daemon = daemon

    async def profile_id(self) -> int | None:
        """The signed-in butler profile, or None when nobody is signed in."""
        result = await self._daemon.call("Profile.List")
        profiles = result.get("profiles") or []
        return int(profiles[0]["id"]) if profiles else None

    async def get_library(self) -> list[Game] | None:
        """Owned, runnable games with install state; None when unreadable."""
        try:
            profile = await self.profile_id()
            if profile is None:
                return None
            owned = await self._owned_games(profile)
            collected = await self._free_collection_games(profile)
            installs = await self.installed_map()
        except (ButlerdError, OSError, TimeoutError) as e:
            logger.warning("[itch] library read failed: %s", e)
            return None
        owned_ids = {g["id"] for g in owned}
        extra = {g["id"]: g for g in collected if g["id"] not in owned_ids}
        candidates = [g for g in (*owned, *extra.values()) if is_game(g)]
        try:
            kinds = await self._classify_untagged(
                [g for g in candidates if not is_runnable_game(g)],
            )
        except (ButlerdError, OSError, TimeoutError) as e:
            logger.warning("[itch] upload lookup for untagged games failed: %s", e)
            return None
        games = [
            to_game(g, owned=g["id"] in owned_ids, web=kinds.get(g["id"]) == "web")
            for g in candidates if is_runnable_game(g) or kinds.get(g["id"])
        ]
        logger.info("[itch] %d owned + %d free collection → %d games (%d web), %d installed",
                    len(owned), len(extra), len(games),
                    sum(1 for k in kinds.values() if k == "web"), len(installs))
        return merge_install_status(games, installs)

    async def _classify_untagged(self, games: list[dict[str, Any]]) -> dict[int, str]:
        """``{game_id: "download" | "web"}`` for games whose page names no platform.

        One uploads lookup per *untagged* game only; tagged games cost
        nothing. "download": an upload whose filename names a Linux or Windows
        build. "web": only an HTML5 build, which is launched in Edge rather
        than installed. Neither → left out.
        """
        gate = asyncio.Semaphore(_UNTAGGED_CONCURRENCY)

        async def one(game: dict[str, Any]) -> tuple[int, str | None]:
            async with gate:
                result = await self._call_with_backoff("Fetch.GameUploads", {
                    "gameId": game["id"], "compatible": False, "fresh": True,
                })
            uploads = result.get("uploads") or []
            if choose_upload(uploads) is not None:
                return game["id"], "download"
            return game["id"], "web" if has_web_build(uploads) else None

        pairs = await asyncio.gather(*(one(g) for g in games))
        return {gid: kind for gid, kind in pairs if kind}

    async def owned_game(self, game_id: str) -> dict[str, Any] | None:
        """The full butlerd ``Game`` object for one owned game."""
        result = await self._daemon.call(
            "Fetch.Game", {"gameId": int(game_id), "fresh": True},
        )
        game = result.get("game")
        return game if isinstance(game, dict) else None

    async def installed_map(self) -> dict[str, dict[str, Any]]:
        """``{game_id: {"install_path", "cave_id", "upload", "title"}}`` from caves."""
        caves = await self._paged("Fetch.Caves", {}, fresh=False)
        return dict(filter(None, (_cave_entry(c) for c in caves)))

    async def _owned_games(self, profile: int) -> list[dict[str, Any]]:
        items = await self._paged("Fetch.ProfileOwnedKeys", {"profileId": profile})
        return [item["game"] for item in items if isinstance(item.get("game"), dict)]

    async def _free_collection_games(self, profile: int) -> list[dict[str, Any]]:
        """Free games from every collection the user owns (see module docstring)."""
        games: list[dict[str, Any]] = []
        for collection in await self._paged("Fetch.ProfileCollections", {"profileId": profile}):
            items = await self._paged("Fetch.Collection.Games", {
                "profileId": profile, "collectionId": collection["id"],
            })
            games.extend(
                item["game"] for item in items
                if isinstance(item.get("game"), dict) and not item["game"].get("minPrice")
            )
        return games

    async def _paged(
        self, method: str, base: dict[str, Any], *, fresh: bool = True,
    ) -> list[dict[str, Any]]:
        """Every page of a cursor-paged butlerd fetch.

        ``fresh`` asks butler to go to itch.io instead of its cache; the
        local-only reads (``Fetch.Caves``) take no such flag.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {**base, "limit": _PAGE_LIMIT, **({"fresh": True} if fresh else {})}
            if cursor:
                params["cursor"] = cursor
            result = await self._call_with_backoff(method, params)
            items.extend(result.get("items") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    async def _call_with_backoff(
        self, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        for delay in (*_BACKOFF_S, None):
            try:
                return await self._daemon.call(method, params, timeout=180)
            except ButlerdError as e:
                if e.api_status not in _RATE_LIMIT_STATUSES or delay is None:
                    raise
                logger.info("[itch] %s rate-limited (%s); retrying in %.0fs",
                            method, e.api_status, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")
