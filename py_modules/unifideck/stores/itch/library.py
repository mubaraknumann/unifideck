"""itch.io library: owned keys → ``Game`` rows, with butler's installs overlaid.

The two butlerd reads behave in ways the docs do not warn about (measured on
butler 15.31.0, 2026-09-23):

* **``fresh: true`` on the first page, and only there.** Without it the
  answer comes from butler's local cache, which on a new database is empty,
  and "empty" arrives as ``[]``, not as an error. Returned as-is, that would
  tell the reconcile the user owns nothing and delete every itch.io shortcut
  (stores.md invariant 2). But butler re-walks the *whole* remote list on
  every fresh call and ignores the cursor (``lazyfetch.Do``), so sending it
  on every page made a read cost pages × full walk. A ~1,700-key library
  never finished inside the sync's 120s cap (two tester bundles, 2026-10).
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

Games in a **bundle** the user bought are listed too, before they are claimed
(``bundles.py``). They carry ``GameTag.CLAIM_REQUIRED`` until the claim turns
them into owned keys.

Any failure here returns ``None`` ("could not read"), never ``[]``. That includes
a failed collections read, or the collection games' shortcuts would be
deleted on a flaky sync.

A large library can still take longer than one sync is willing to wait.
The read runs as its own task: a sync waits up to ``_SYNC_BUDGET_S`` for it,
then answers ``None`` (the sync keeps the existing shortcuts) and lets the
task finish. When it does, ``on_ready`` asks for another sync, which picks
up the finished result instead of starting over.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from unifideck.core.types import Game, GameTag
from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.shared.install_status import merge_install_status

from .bundles import read_bundle_games
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
#: How long one sync waits for the library read before keeping the current
#: shortcuts and letting the read finish in the background. Kept under the
#: sync's per-store cap (``PER_STORE_FETCH_TIMEOUT_SECONDS``, 120s), which
#: would otherwise cancel the read and throw its progress away.
_SYNC_BUDGET_S = 90.0


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


def runnable_platforms(game: dict[str, Any]) -> list[str]:
    """Linux/Windows, from the page's tags or itch.io's upload scan.

    ``scannedPlatforms`` holds entries such as ``linux-amd64`` or
    ``windows-386``, found by scanning the uploads; it is absent until a
    scan ran. It matters for bundle games: an unclaimed paid game lists no
    uploads, so the scan is the only build evidence besides the tags.
    """
    tagged = game.get("platforms") or {}
    scanned = {str(s).split("-", 1)[0] for s in game.get("scannedPlatforms") or ()}
    return sorted(p for p in _RUNNABLE_PLATFORMS if tagged.get(p) or p in scanned)


def to_game(
    game: dict[str, Any], *, source: str = "owned", web: bool = False,
    bundle: dict[str, Any] | None = None,
) -> Game:
    """Map a butlerd ``Game`` object to our record.

    *source* is "owned" (an owned key: bought or claimed), "collection" (a
    free game the user bookmarked in one of their collections) or "bundle"
    (in a bundle the user bought, not yet claimed; *bundle* is that bundle).

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
        "source": source,
        "platforms": runnable_platforms(game),
    }
    tags: list[str] = []
    if web and game.get("url"):
        metadata[BROWSER_URL_KEY] = game["url"]
        tags.append(GameTag.BROWSER)
    if bundle is not None:
        metadata["bundle_id"] = bundle.get("id")
        metadata["bundle_title"] = str(bundle.get("title") or "")
        tags.append(GameTag.CLAIM_REQUIRED)
    return Game(
        app_id=0,
        store=STORE,
        store_game_id=str(game["id"]),
        title=str(game.get("title") or f"itch.io game {game['id']}"),
        icon_url=game.get("coverUrl") or game.get("stillCoverUrl") or None,
        tags=tags,
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

    def __init__(
        self, daemon: ButlerDaemon,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._daemon = daemon
        #: Asked for another sync when a read a sync stopped waiting for
        #: finishes (module docstring).
        self._on_ready = on_ready
        #: The library read in flight, or finished and not yet handed to a
        #: sync. Shared by every sync until one takes its result.
        self._read: asyncio.Task[list[Game]] | None = None
        #: A sync stopped waiting for ``_read``.
        self._read_abandoned = False
        self._ready_tasks: set[asyncio.Task[None]] = set()
        #: ``{game_id: "download" | "web" | None}`` for untagged games, kept
        #: for the life of the plugin. butler's own upload cache lasts two
        #: minutes, so without this every sync re-asked itch.io for every
        #: untagged game.
        self._kinds: dict[int, str | None] = {}
        #: The last library read failed because itch.io refused the key (403:
        #: scoped or revoked). Signing in again is the fix, so the store tells
        #: the user instead of only logging it.
        self.key_rejected = False

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
            games = await self._wait_for_read(profile)
            if games is None:
                return None
            installs = await self.installed_map()
        except (ButlerdError, OSError, TimeoutError) as e:
            logger.warning("[itch] library read failed: %s", e)
            self.key_rejected = isinstance(e, ButlerdError) and e.api_status == 403
            return None
        self.key_rejected = False
        logger.info("[itch] %d games, %d installed", len(games), len(installs))
        return merge_install_status(games, installs)

    async def _wait_for_read(self, profile: int) -> list[Game] | None:
        """The read's games, or None when it outlasts ``_SYNC_BUDGET_S``.

        A sync that gives up, or is cancelled by the sync's own cap, leaves
        the read running. A read that raises is dropped (``read.result()``
        re-raises it here), so the next sync starts a new one.
        """
        if self._read is None:
            self._read = asyncio.create_task(
                self._read_games(profile), name="itch-library-read",
            )
            self._read.add_done_callback(self._on_read_done)
        read = self._read
        # ``asyncio.wait`` neither cancels the read on timeout nor when this
        # sync is itself cancelled, so the read always carries on.
        await asyncio.wait({read}, timeout=_SYNC_BUDGET_S)
        if not read.done():
            self._read_abandoned = True
            logger.warning(
                "[itch] library still loading after %.0fs; keeping the current "
                "itch.io shortcuts and syncing again when it finishes",
                _SYNC_BUDGET_S,
            )
            return None
        self._read = None
        self._read_abandoned = False
        return read.result()

    def _on_read_done(self, task: asyncio.Task[list[Game]]) -> None:
        """Ask for a sync when a read nobody is waiting for succeeds."""
        if task.cancelled():
            return
        if task.exception() is not None:
            # Retrieved here so asyncio does not log it as never retrieved;
            # the sync that takes this task re-raises and logs it.
            return
        if self._read_abandoned and self._on_ready is not None:
            self._read_abandoned = False
            logger.info("[itch] library finished loading in the background")
            ready = asyncio.ensure_future(self._on_ready())
            self._ready_tasks.add(ready)
            ready.add_done_callback(self._ready_tasks.discard)

    async def cancel_read(self) -> None:
        """Stop a read in flight (plugin unload)."""
        if self._read is not None and not self._read.done():
            self._read.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read
        self._read = None

    async def _read_games(self, profile: int) -> list[Game]:
        """Every listable game, without install state."""
        started = time.monotonic()
        owned = await self._owned_games(profile)
        collected = await self._free_collection_games(profile)
        bundled = await read_bundle_games(self._paged, self._call_with_backoff, profile)
        listed = time.monotonic()
        owned_ids = {g["id"] for g in owned}
        extra = {g["id"]: g for g in collected if g["id"] not in owned_ids}
        candidates = [g for g in (*owned, *extra.values()) if is_game(g)]
        untagged = [g for g in candidates if not is_runnable_game(g)]
        kinds = await self._classify_untagged(untagged)
        games = [
            to_game(g, source="owned" if g["id"] in owned_ids else "collection",
                    web=kinds.get(g["id"]) == "web")
            for g in candidates if is_runnable_game(g) or kinds.get(g["id"])
        ]
        # Claimed games are owned keys, and free ones install as collection
        # games; what is left needs a claim before it can install.
        unclaimed = [(g, b) for g, b in bundled
                     if is_game(g) and g["id"] not in owned_ids and g["id"] not in extra]
        claimable = [(g, b) for g, b in unclaimed if runnable_platforms(g)]
        games += [to_game(g, source="bundle", bundle=b) for g, b in claimable]
        logger.info(
            "[itch] %d owned + %d free collection + %d unclaimed bundle → %d games "
            "(%d web; left out: %d untagged, %d bundle with no Linux/Windows build); "
            "lists %.1fs, uploads %.1fs",
            len(owned), len(extra), len(unclaimed), len(games),
            sum(1 for g in untagged if kinds.get(g["id"]) == "web"),
            sum(1 for g in untagged if not kinds.get(g["id"])),
            len(unclaimed) - len(claimable),
            listed - started, time.monotonic() - listed,
        )
        return games

    async def _classify_untagged(self, games: list[dict[str, Any]]) -> dict[int, str]:
        """``{game_id: "download" | "web"}`` for games whose page names no platform.

        One uploads lookup per *untagged* game, once per plugin run (see
        ``_kinds``); tagged games cost nothing. "download": an upload whose
        filename names a Linux or Windows build. "web": only an HTML5
        build, which is launched in Edge rather than installed. Neither →
        left out.
        """
        gate = asyncio.Semaphore(_UNTAGGED_CONCURRENCY)

        async def one(game: dict[str, Any]) -> None:
            async with gate:
                uploads = await self._uploads(game["id"])
            if choose_upload(uploads) is not None:
                self._kinds[game["id"]] = "download"
            else:
                self._kinds[game["id"]] = "web" if has_web_build(uploads) else None

        await asyncio.gather(*(one(g) for g in games if g["id"] not in self._kinds))
        return {g["id"]: kind for g in games if (kind := self._kinds.get(g["id"]))}

    async def _uploads(self, game_id: int) -> list[dict[str, Any]]:
        """butler's cached uploads, fetched from itch.io only when stale."""
        params: dict[str, Any] = {"gameId": game_id, "compatible": False}
        result = await self._call_with_backoff("Fetch.GameUploads", params)
        if result.get("stale"):
            result = await self._call_with_backoff(
                "Fetch.GameUploads", {**params, "fresh": True},
            )
        return list(result.get("uploads") or [])

    async def owned_game(self, game_id: str) -> dict[str, Any] | None:
        """The full butlerd ``Game`` object for one owned game."""
        result = await self._daemon.call(
            "Fetch.Game", {"gameId": int(game_id), "fresh": True},
        )
        game = result.get("game")
        return game if isinstance(game, dict) else None

    async def ownership_source(self, profile: int, game_id: str) -> str:
        """How *profile* owns *game_id*: "download_key", "bundle" or "".

        ``Fetch.GameOwnership`` is local-only. "bundle" means the game is in
        a bundle the user bought and has not been claimed yet (as of the
        last sync's owned-keys read).
        """
        result = await self._daemon.call(
            "Fetch.GameOwnership", {"profileId": profile, "gameId": int(game_id)},
        )
        return str(result.get("source") or "")

    async def bundle_title(self, profile: int, game_id: str) -> str:
        """Title of the owned bundle *game_id* is in, or "" (local reads)."""
        result = await self._daemon.call(
            "Fetch.GameOwnership", {"profileId": profile, "gameId": int(game_id)},
        )
        bundle_id = result.get("bundleId")
        if not bundle_id:
            return ""
        for key in await self._paged("Fetch.ProfileOwnedBundles", {"profileId": profile}, fresh=False):
            bundle = key.get("bundle") or {}
            if bundle.get("id") == bundle_id:
                return str(bundle.get("title") or "")
        return ""

    async def refresh_owned_keys(self, profile: int) -> None:
        """Re-read the owned keys from itch.io (one full walk, see module docstring).

        After the user claims a bundle game on the page, this is what puts
        its new download key in butler's database.
        """
        await self._call_with_backoff(
            "Fetch.ProfileOwnedKeys", {"profileId": profile, "limit": 1, "fresh": True},
            call_timeout_s=600,
        )

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

        ``fresh`` asks butler to go to itch.io instead of its cache, and is
        sent on the first page only: that call re-walks the whole remote
        list into butler's database, and the later pages read it from there
        (module docstring). The local-only reads (``Fetch.Caves``) take no
        such flag.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {**base, "limit": _PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            elif fresh:
                params["fresh"] = True
            result = await self._call_with_backoff(method, params)
            items.extend(result.get("items") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    async def _call_with_backoff(
        self, method: str, params: dict[str, Any], *, call_timeout_s: float = 180,
    ) -> dict[str, Any]:
        for delay in (*_BACKOFF_S, None):
            try:
                return await self._daemon.call(method, params, timeout=call_timeout_s)
            except ButlerdError as e:
                if e.api_status not in _RATE_LIMIT_STATUSES or delay is None:
                    raise
                logger.info("[itch] %s rate-limited (%s); retrying in %.0fs",
                            method, e.api_status, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")
