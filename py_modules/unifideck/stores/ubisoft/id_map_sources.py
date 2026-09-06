"""
Crowd-sourced Ubisoft game-ID lookup tables — download & cache.

Ubisoft does not publish a public mapping from UPC ``space_id`` to
human-readable game name; we rely on a community-maintained list hosted
on GitHub (``iArtorias/ubisoft_game_ids``).

This module:

* downloads and caches the list with TTL (``game_id_db_max_age_seconds``);
* parses the file (one ``space_id|name`` per line) into a dict;
* exposes ``lookup(space_id)`` for the library facade to call when it
  finds an installed game whose name isn't in the local UPC catalog.

Network failures are degraded gracefully — a stale cache is preferred
to no cache, and a missing cache is preferred to a hard error: in both
cases the lookup falls back to an empty dict and the library facade
displays "Ubisoft Game" as a placeholder name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.net import ssl_ctx_permissive

if TYPE_CHECKING:
    from .id_map import UbisoftIdMap
logger = logging.getLogger(__name__)
# uuid (appId/spaceId) → name catalog, built weekly from Ubisoft Connect's
# public Algolia product index and served from unifiDB via jsDelivr. Names the
# modern owned games the legacy install_id list lacks. Cached next to the
# install_id DB with the same TTL.
_UUID_CATALOG_URL = (
    "https://cdn.jsdelivr.net/gh/mubaraknumann/unifiDB@main/ubisoft/uuid_catalog.json"
)
_UUID_CATALOG_FILENAME = "ubisoft_uuid_catalog.json"
_REGISTRY_INSTALLS_PATTERN = re.compile(
    r"\[Software\\\\Wow6432Node\\\\Ubisoft\\\\Launcher"
    r"\\\\Installs\\\\(\d+)\]"
    r'[^\[]*?"InstallDir"\s*=\s*"([^"]*)"',
    re.DOTALL,
)
_USER_REG_INSTALLS_PATTERN = re.compile(
    r"\[Software\\\\Ubisoft\\\\Launcher\\\\Installs\\\\(\d+)\]",
)
_STANDARD_INSTALL_PATH_MARKERS = (
    "Ubisoft Game Launcher/games/",
    "Ubisoft Game Launcher\\games\\",
)
def extract_game_id_from_registry(
    prefix_path: str,
) -> str | None:
    """Extract game ID from registry."""
    prefix = Path(prefix_path)
    for reg_name in ("system.reg", "pfx/system.reg"):
        reg_path = prefix / reg_name
        if not reg_path.is_file():
            continue
        content = read_reg_file(str(reg_path))
        if content is None:
            continue
        system_id = scan_system_reg_installs(content)
        if system_id:
            return system_id
        user_id = extract_id_from_user_reg_sibling(
            str(reg_path),
        )
        if user_id:
            return user_id
    return None

def read_reg_file(reg_path: str) -> str | None:
    """Read reg file."""
    try:
        return Path(reg_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None

def scan_system_reg_installs(content: str) -> str | None:
    """Scan system reg installs."""
    fallback_id: str | None = None
    for match in _REGISTRY_INSTALLS_PATTERN.finditer(content):
        game_id = match.group(1)
        install_dir = match.group(2).replace("\\\\", "/")
        is_standard = any(
            marker in install_dir for marker in _STANDARD_INSTALL_PATH_MARKERS
        )
        if is_standard:
            logger.info(
                "[UbisoftIdMap] registry ID %s (standard path)",
                game_id,
            )
            return game_id
        if fallback_id is None:
            fallback_id = game_id
    if fallback_id:
        logger.info(
            "[UbisoftIdMap] registry ID %s (custom install path)",
            fallback_id,
        )
        return fallback_id
    return None

def extract_id_from_user_reg_sibling(
    reg_path: str,
) -> str | None:
    """Extract ID from user reg sibling."""
    user_reg = reg_path.replace("system.reg", "user.reg")
    if not Path(user_reg).is_file():
        return None
    user_content = read_reg_file(user_reg)
    if user_content is None:
        return None
    user_match = _USER_REG_INSTALLS_PATTERN.search(
        user_content,
    )
    if user_match:
        game_id = user_match.group(1)
        logger.info(
            "[UbisoftIdMap] registry ID %s (user.reg)",
            game_id,
        )
        return game_id
    return None

class _IdMapSources:
    """Id map sources."""

    def __init__(self, idmap: UbisoftIdMap) -> None:
        """Initialize the instance."""
        self._idmap = idmap

    async def refresh_from_configurations(
        self,
        space_id: str | None = None,
    ) -> bool:
        """Refresh from configurations."""
        try:
            # Fix (2026-05-15, lot 11e): the module is
            # ``unifideck.stores.ubisoft.parser`` (dotted), not
            # ``unifideck.stores.ubisoft_parser`` — the previous
            # path was a typo that mypy strict flagged as
            # ``import-not-found``. At runtime the same path
            # would have raised the ImportError caught below,
            # silently skipping the refresh.
            from unifideck.stores.ubisoft.parser import build_id_map_from_configurations
        except ImportError as e:
            logger.warning(
                "[UbisoftIdMap] ubisoft.parser unavailable: %s",
                e,
            )
            return False
        config = self._idmap._config
        paths = self._idmap._paths
        template_dir = config.template_dir_expanded
        config_path = await asyncio.to_thread(
            paths.find_configurations, template_dir,
        )
        if config_path and await self._refresh_from_path(
            config_path,
            build_id_map_from_configurations,
            "template",
        ):
            return True
        # Union of the internal prefixes_dir scan and any per-game prefixes
        # relocated to SD / custom storage — a freshly-installed game's
        # configurations (which carry its launch_id) may live in an external
        # prefix.
        prefix_paths = await asyncio.to_thread(
            self._idmap.iter_all_game_prefix_paths,
        )
        if not prefix_paths:
            logger.info(
                "[UbisoftIdMap] no configurations found in any prefix",
            )
            return False
        for prefix_str in prefix_paths:
            entry = Path(prefix_str)
            if entry.name.startswith("."):
                continue
            config_path = await asyncio.to_thread(
                paths.find_configurations, str(entry),
            )
            if not config_path:
                continue
            if await self._refresh_from_path(
                config_path,
                build_id_map_from_configurations,
                f"prefix {entry.name}",
            ):
                return True
        logger.info(
            "[UbisoftIdMap] no configurations found in any prefix",
        )
        return False

    async def _refresh_from_path(
        self,
        config_path: str,
        parser_fn: Any,
        label: str,
    ) -> bool:
        """Refresh from path."""
        try:
            new_map = await asyncio.to_thread(
                parser_fn,
                config_path,
            )
        except Exception as e:
            logger.warning(
                "[UbisoftIdMap] parser failed for %s: %s",
                label,
                e,
            )
            return False
        if not new_map:
            return False
        before_count = len(self._idmap._cache)
        self._idmap.update_bulk(new_map)
        after_count = len(self._idmap._cache)
        logger.info(
            "[UbisoftIdMap] refreshed from %s: %d entries (was %d)",
            label,
            after_count,
            before_count,
        )
        return True

    async def fetch_game_id_database(
        self,
        force: bool = False,
    ) -> list[tuple[str, str]]:
        """Fetch game ID database.

        ``force`` (a force-sync) bypasses the TTL cache and re-downloads the
        latest list from unifiDB; a regular sync uses the weekly cache.
        """
        config = self._idmap._config
        cache_file = config.game_id_db_file_expanded
        max_age = config.game_id_db_max_age_seconds
        cache_p = Path(cache_file)
        if not force and await asyncio.to_thread(cache_p.is_file):
            with contextlib.suppress(OSError):
                # Bug fix (lot 12c): the previous line read
                # ``time.time() - await asyncio.to_thread(cache_p.stat).st_mtime``
                # which Python parses as
                # ``time.time() - await (to_thread(...).st_mtime)`` —
                # but ``to_thread()`` returns a *coroutine*, not the
                # stat_result, so ``.st_mtime`` would raise
                # ``AttributeError`` at runtime. The OSError suppress
                # swallowed AttributeError silently (it doesn't — OSError
                # is unrelated), so the path was effectively dead and
                # the cache was never read from disk. Parenthesise the
                # await so the coroutine is resolved first.
                stat_result = await asyncio.to_thread(cache_p.stat)
                age = time.time() - stat_result.st_mtime
                if age < max_age:
                    return await asyncio.to_thread(
                        _parse_game_id_database,
                        cache_file,
                    )
        try:
            await asyncio.to_thread(
                _download_game_id_database,
                config.game_id_db_url,
                cache_file,
            )
            logger.info(
                "[UbisoftIdMap] game ID database downloaded",
            )
        except Exception as e:
            logger.warning(
                "[UbisoftIdMap] game ID database download failed: %s",
                e,
            )
            if not await asyncio.to_thread(cache_p.is_file):
                return []
        return await asyncio.to_thread(
            _parse_game_id_database,
            cache_file,
        )

    async def fetch_uuid_catalog(
        self,
        force: bool = False,
    ) -> dict[str, str]:
        """``uuid (appId/spaceId) → name`` from unifiDB's Ubisoft catalog.

        Names the modern owned games stored in the ownership binary by UUID
        (the legacy install_id list can't). Cached with the same TTL as the
        install_id DB; ``force`` (force-sync) bypasses it. Degrades to ``{}``
        on any network/parse failure.
        """
        config = self._idmap._config
        cache_file = str(
            Path(config.data_dir_expanded) / _UUID_CATALOG_FILENAME,
        )
        cache_p = Path(cache_file)
        if not force and await asyncio.to_thread(cache_p.is_file):
            with contextlib.suppress(OSError):
                stat_result = await asyncio.to_thread(cache_p.stat)
                if time.time() - stat_result.st_mtime < config.game_id_db_max_age_seconds:
                    return await asyncio.to_thread(_parse_uuid_catalog, cache_file)
        try:
            await asyncio.to_thread(
                _download_game_id_database,
                _UUID_CATALOG_URL,
                cache_file,
            )
            logger.info("[UbisoftIdMap] uuid catalog downloaded")
        except Exception as e:
            logger.warning(
                "[UbisoftIdMap] uuid catalog download failed: %s", e,
            )
            if not await asyncio.to_thread(cache_p.is_file):
                return {}
        return await asyncio.to_thread(_parse_uuid_catalog, cache_file)

    async def lookup_game_id_by_name(
        self,
        game_name: str,
    ) -> str | None:
        """Lookup game ID by name."""
        if not game_name:
            return None
        try:
            db_entries = await self.fetch_game_id_database()
        except Exception as e:
            logger.debug(
                "[UbisoftIdMap] fetch failed for name lookup: %s",
                e,
            )
            return None
        if not db_entries:
            return None
        normalized_query = self._idmap._normalize_for_matching(game_name)
        for install_id, db_name in db_entries:
            if (
                self._idmap._normalize_for_matching(
                    db_name,
                )
                == normalized_query
            ):
                logger.info(
                    "[UbisoftIdMap] DB match for '%s': ID %s",
                    game_name,
                    install_id,
                )
                return install_id
        return None

def _download_game_id_database(
    url: str,
    dest_path: str,
) -> None:
    """Download game ID database."""
    dest_p = Path(dest_path)
    tmp_path = dest_p.with_suffix(dest_p.suffix + ".tmp")
    ctx = ssl_ctx_permissive(
        "Ubisoft community game ID database — CDN has known "
        "stale certs, payload treated as advisory only",
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Unifideck/1.0"},
    )
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    with (
        urllib.request.urlopen(
            req,
            timeout=30.0,
            context=ctx,
        ) as response,
        tmp_path.open("wb") as f,
    ):
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            f.write(chunk)
    tmp_path.replace(dest_p)

def _parse_uuid_catalog(filepath: str) -> dict[str, str]:
    """Parse unifiDB's ``uuid_catalog.json`` into ``uuid → name``.

    The file shape is ``{"games": {"<uuid>": {"name": ..., ...}}}``. Any
    read/parse error (or unexpected shape) degrades to an empty dict.
    """
    try:
        data = json.loads(
            Path(filepath).read_text(encoding="utf-8", errors="replace"),
        )
    except (OSError, ValueError) as e:
        logger.warning("[UbisoftIdMap] uuid catalog parse failed: %s", e)
        return {}
    games = data.get("games") if isinstance(data, dict) else None
    if not isinstance(games, dict):
        return {}
    out: dict[str, str] = {}
    for uuid, meta in games.items():
        name = meta.get("name") if isinstance(meta, dict) else None
        if isinstance(uuid, str) and isinstance(name, str) and name:
            out[uuid] = name
    return out

def _parse_game_id_database(
    filepath: str,
) -> list[tuple[str, str]]:
    """Parse game ID database."""
    entries: list[tuple[str, str]] = []
    try:
        content = Path(filepath).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        logger.warning(
            "[UbisoftIdMap] database parse failed: %s",
            e,
        )
        return entries
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(", ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            entries.append((parts[0], parts[1]))
    return entries
