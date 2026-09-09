"""Amazon Games library reader — owned games list + install status merger.

``AmazonLibraryReader`` reads the user's owned-games list from the
``nile`` user data file (the JSON state that nile maintains after a
successful login).

Public methods :

* ``fetch_owned_games()`` — load the games list from the user file;
* ``ensure_user_file_present()`` — check the file exists, warn the
  user if not;
* ``parse_entries(data)`` — extract game records from raw JSON;
* ``check_user_data_freshness()`` — TTL-aware freshness check.

Overlaying installed-state (from the install registry) onto the
owned-games list is ``stores/shared/install_status.merge_install_status``,
shared with Epic and GOG (audit §3.4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.core.io import async_file_ops as aio
from unifideck.core.types import Game

logger = logging.getLogger(__name__)

class AmazonLibraryReader:
    """Amazon library reader."""

    def __init__(self, config_dir: str) -> None:
        """Initialize the instance."""
        config_path = Path(config_dir).expanduser()
        self._config_dir = str(config_path)
        self._library_path = str(config_path / "library.json")
        self._installed_path = str(
            config_path / "installed.json",
        )

    async def read_owned_games(self) -> list[Game]:
        """Read owned games."""
        data = await self._read_json(self._library_path)
        if not isinstance(data, list):
            return []
        games: list[Game] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            product = item.get("product")
            if not isinstance(product, dict):
                continue
            game_id = product.get("id", "")
            if not game_id:
                continue
            games.append(
                Game(
                    app_id=0,
                    store="amazon",
                    store_game_id=game_id,
                    title=str(product.get("title") or game_id),
                    installed=False,
                )
            )
        logger.info(
            "[amazon_library] %d owned games",
            len(games),
        )
        return games

    async def sync_library(
        self,
        cli_path: str,
        timeout: int,  # noqa: ASYNC109 — passed to asyncio.wait_for
    ) -> bool:
        """Refresh ``library.json`` from Amazon via ``nile library sync``.

        The plugin otherwise only ever *reads* the on-disk
        ``library.json``; nile only (re)writes it at login/register. So
        games claimed after the last login never enter the file and never
        appear (UD-012). Mirroring Epic/GOG, we re-fetch the owned list on
        every sync. Never raises: on any failure we return ``False`` and the
        caller falls through to the last-known (possibly stale) file, so the
        user still gets their existing library.
        """
        env = self._sync_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                cli_path,
                "library",
                "sync",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except (TimeoutError, OSError) as e:
            logger.warning("[amazon_library] library sync failed: %s", e)
            return False
        if proc.returncode != 0:
            logger.warning(
                "[amazon_library] library sync rc=%s: %s",
                proc.returncode,
                stderr.decode(errors="ignore")[:200],
            )
            return False
        logger.info("[amazon_library] library synced")
        return True

    def _sync_env(self) -> dict[str, str]:
        """Env for ``nile library sync`` targeting ``self._config_dir``.

        nile resolves its config dir from ``XDG_CONFIG_HOME/nile`` (else
        ``$HOME/.config/nile``). When our config dir already equals the
        ambient default we leave ``XDG_CONFIG_HOME`` alone; when it differs
        we point it at the parent so nile reads auth from and writes
        ``library.json`` to the same dir the reader reads. nile can only be
        redirected to a ``nile``-basename dir.

        Always built from ``clean_cli_env`` rather than inherited verbatim:
        the Decky backend is PyInstaller-frozen and its ``os.environ``
        carries ``LD_LIBRARY_PATH=/tmp/_MEIxxxx``, which makes a spawned
        binary link the loader's libraries instead of its own.
        """
        target = Path(self._config_dir)
        xdg = os.environ.get("XDG_CONFIG_HOME")
        ambient = (
            Path(xdg).expanduser() / "nile"
            if xdg
            else Path("~/.config/nile").expanduser()
        )
        if target == ambient:
            return clean_cli_env()
        if target.name != "nile":
            logger.debug(
                "[amazon_library] non-nile config dir %s; inheriting env",
                target,
            )
            return clean_cli_env()
        return clean_cli_env({"XDG_CONFIG_HOME": str(target.parent)})

    async def read_installed_ids(self) -> dict[str, dict[str, Any]]:
        """Read installed ids."""
        data = await self._read_json(self._installed_path)
        if not isinstance(data, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            game_id = entry.get("id")
            if not game_id:
                continue
            result[game_id] = {
                "path": entry.get("path", ""),
                "version": entry.get("version", ""),
            }
        logger.info(
            "[amazon_library] %d installed games",
            len(result),
        )
        return result

    async def get_official_url(self, game_id: str) -> str | None:
        """Get official URL."""
        data = await self._read_json(self._library_path)
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict):
                continue
            product = item.get("product", {})
            if product.get("id") != game_id:
                continue
            details = product.get("productDetail", {}).get("details", {})
            websites = details.get("websites", {})
            for key in ("OFFICIAL", "STEAM"):
                url = websites.get(key)
                if isinstance(url, str) and url:
                    return url
            return None
        return None

    async def _read_json(self, path: str) -> Any:
        """Read JSON."""
        try:
            if not await aio.is_file(path):
                return None
            content = await aio.read_text(path)
            if content is None:
                return None
            return json.loads(content)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(
                "[amazon_library] read %s failed: %s",
                path,
                e,
            )
            return None
