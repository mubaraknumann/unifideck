"""RomM catalog: PC-only games, complete or not at all."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from unifideck.core.types import Game

from .auth import ALLOWED_PLATFORM_SLUGS, STORE_NAME, RomMAuth, eligible_variants
from .client import RomMAPIError, RomMClient
from .identity import rom_id_for_server, scoped_game_id

logger = logging.getLogger(__name__)

_PAGE_SIZE = 500


def epoch_to_iso_date(value: Any) -> str | None:
    """RomM sends ``first_release_date`` as epoch seconds; consumers want ISO."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _rom_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RomMAPIError("RomM returned a ROM without a valid ID", code="malformed")
    return int(value)


def _parse_page(data: Any) -> tuple[list[dict[str, Any]], int | None]:
    page = data.get("items") if isinstance(data, dict) else None
    total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(page, list) or not all(isinstance(i, dict) for i in page):
        raise RomMAPIError("RomM returned an unexpected ROM page", code="malformed")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise RomMAPIError("RomM returned an unexpected ROM page", code="malformed")
    return page, total


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _same_game(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two entries are the same game on different platforms.

    Every field both entries carry an ID for is compared, and any mismatch
    means different games, even when the names are identical. Only when no
    field has an ID on both sides does the exact title decide, which keeps a
    Windows build from being hidden behind an unrelated Linux game that happens
    to share its name.
    """
    comparable = False
    for field in ("igdb_id", "ss_id"):
        left_id, right_id = left.get(field), right.get(field)
        if not (
            type(left_id) is int and left_id > 0
            and type(right_id) is int and right_id > 0
        ):
            continue
        comparable = True
        if left_id != right_id:
            return False
    if comparable:
        return True
    name = _text(left.get("name"))
    return bool(name) and name == _text(right.get("name"))


def _cover_url(item: dict[str, Any], server_url: str) -> str | None:
    for field in ("path_cover_large", "path_cover_small", "url_cover"):
        candidate = _text(item.get(field))
        if not candidate:
            continue
        try:
            parts = urlsplit(candidate)
            if parts.netloc and not parts.scheme:
                continue
            # RomM's resource paths start with '/', but a reverse proxy may
            # mount the whole server under a prefix in the configured URL.
            url = urljoin(f"{server_url}/", candidate if parts.scheme else candidate.lstrip("/"))
            parts = urlsplit(url)
            if parts.scheme in ("http", "https") and parts.hostname and not (parts.username or parts.password):
                return url
        except ValueError:
            continue
    return None


class RomMCatalog:
    """RomM's game list, and the ROM records behind each game ID.

    Keeps the ROMs from the last fetch so an install can resolve a file name
    without a second request. That cache is dropped whenever the connected
    server changes, because the IDs only mean anything within one server.
    """

    def __init__(self, auth: RomMAuth) -> None:
        self._auth = auth
        self._roms: dict[str, dict[str, Any]] = {}
        self._cache_server = ""

    def _use_server(self, server_url: str) -> None:
        if server_url != self._cache_server:
            self._roms = {}
            self._cache_server = server_url

    async def fetch(self) -> list[Game]:
        """Every supported game, without Windows duplicates of Linux builds.

        Raises rather than returning part of the library: a partial catalog is
        read by the sync as a library that shrank, and the missing games'
        shortcuts get swept. A library with no importable platform is a real
        answer rather than a failure, and returns ``[]`` without querying ROMs,
        because an empty platform filter would ask RomM for every console it
        holds.
        """
        client = self._auth.client()
        self._use_server(client.server_url)
        excluded = set(self._auth.excluded_fs_slugs)
        variants = [
            v for v in eligible_variants(await client.get_json("api/platforms"))
            if v["fs_slug"] not in excluded
        ]
        if not variants:
            # The library really is empty, not unread, so the cache is cleared.
            self._roms = {}
            return []

        platform_params = [("platform_ids", str(v["id"])) for v in variants]
        items = await self._fetch_pages(client, platform_params)
        roms: dict[str, dict[str, Any]] = {}
        for item in items:
            roms[scoped_game_id(client.server_url, _rom_id(item.get("id")))] = item
        self._roms = roms
        return self._build_games(items, client.server_url)

    async def _fetch_pages(self, client: Any, platform_params: list[tuple[str, str]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[int] = set()
        offset = 0
        while True:
            params = [
                *platform_params,
                ("limit", str(_PAGE_SIZE)), ("offset", str(offset)),
                ("order_by", "id"), ("order_dir", "asc"),
                ("with_char_index", "false"), ("with_filter_values", "false"),
                ("with_rom_id_index", "false"),
            ]
            data = await client.get_json("api/roms", params)
            page, total = _parse_page(data)
            for item in page:
                rom_id = _rom_id(item.get("id"))
                if rom_id in seen:
                    raise RomMAPIError("RomM returned overlapping ROM pages", code="malformed")
                seen.add(rom_id)
            items.extend(page)
            if total is not None and len(items) >= total:
                return items
            if not page:
                if total is not None:
                    raise RomMAPIError("RomM ended the ROM list early", code="truncated")
                return items
            offset += len(page)

    def _build_games(self, items: list[dict[str, Any]], server_url: str) -> list[Game]:
        linux_items = [i for i in items if i.get("platform_slug") == "linux"]
        games = []
        for item in items:
            slug = item.get("platform_slug")
            if slug not in ALLOWED_PLATFORM_SLUGS:
                continue
            if slug == "win" and any(_same_game(item, native) for native in linux_items):
                continue
            games.append(self._map(item, server_url))
        return games

    @staticmethod
    def _map(item: dict[str, Any], server_url: str) -> Game:
        rom_id = _rom_id(item.get("id"))
        title = (
            _text(item.get("name")) or _text(item.get("fs_name_no_ext"))
            or _text(item.get("fs_name")) or f"RomM {rom_id}"
        )
        cover = _cover_url(item, server_url)
        meta = item.get("metadatum")
        release = epoch_to_iso_date(meta.get("first_release_date")) if isinstance(meta, dict) else None
        size = item.get("fs_size_bytes")
        metadata: dict[str, Any] = {
            "platform_slug": item.get("platform_slug"),
            "prefer_native": item.get("platform_slug") == "linux",
        }
        if cover:
            metadata["cover_url"] = cover
        if release:
            metadata["release_date"] = release
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            metadata["fs_size_bytes"] = size
        return Game(
            app_id=0, store=STORE_NAME, store_game_id=scoped_game_id(server_url, rom_id), title=title,
            installed=False, icon_url=cover, metadata=metadata,
        )

    async def resolve(self, game_id: str, *, client: RomMClient | None = None) -> dict[str, Any]:
        """The catalog entry for an opaque game ID.

        Resolved through *client* when one is given, so an install started
        before the connection changed keeps using the server it began with.
        """
        client = client or self._auth.client()
        rom_id = rom_id_for_server(client.server_url, game_id)
        self._use_server(client.server_url)
        rom = self._roms.get(game_id)
        if rom is None:
            rom = await client.get_json(f"api/roms/{rom_id}")
            if not isinstance(rom, dict) or _rom_id(rom.get("id")) != rom_id:
                raise RomMAPIError("RomM returned an unexpected ROM", code="malformed")
        if rom.get("platform_slug") not in ALLOWED_PLATFORM_SLUGS:
            raise RomMAPIError("This RomM game is not a Linux or Windows game", code="unsupported_platform")
        if rom.get("platform_fs_slug") in set(self._auth.excluded_fs_slugs):
            raise RomMAPIError("This RomM platform variant is excluded", code="excluded_platform")
        if not _text(rom.get("fs_name")):
            raise RomMAPIError("RomM returned a ROM without a file name", code="malformed")
        self._roms[game_id] = rom
        return rom

    async def size(self, game_id: str) -> int | None:
        try:
            rom = await self.resolve(game_id)
        except RomMAPIError:
            return None
        size = rom.get("fs_size_bytes")
        return size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
