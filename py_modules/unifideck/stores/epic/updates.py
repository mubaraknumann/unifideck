"""Epic Games update checker — on-demand scan via legendary.

``EpicUpdateChecker`` asks ``legendary`` which installed games have a
newer build than the one on disk, and applies an update when asked.

Public methods :

* ``check_for_updates()``   — bulk scan; returns the app names with a
  newer build available. Shells ``legendary list-installed
  --check-updates``, which logs in to Epic and refreshes the asset
  manifest for every installed platform, then reads the
  ``-> Update available!`` lines out of its human-readable output;
* ``update_game(game_id, installer, progress_cb)`` — apply an update;
* ``get_game_size(game_id)`` — cached download size from ``legendary
  info``.

There is no scheduler here: nothing polls. The scan runs when the
frontend asks (``check_game_update``), and
``services/update_check_cache`` keeps that from costing a fresh Epic
login on every App-Details page open.

Update application is delegated to the installer pipeline
(``install.py``) — legendary has no separate ``update`` verb,
it re-runs ``install`` and reuses the existing install directory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, cast

from unifideck.core.binaries import clean_cli_env
from unifideck.core.types import InstallResult
from unifideck.event_bus.event_bus import EventBus

from .legendary import fetch_info
from .library import EpicLibraryReader

logger = logging.getLogger(__name__)

class EpicUpdateChecker:
    """Epic update checker."""

    def __init__(
        self,
        bus: EventBus,
        cli_path: str | None,
        library: EpicLibraryReader,
        list_updates_timeout: int,
        size_cache_ttl: int,
        info_timeout: float,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._cli_path = cli_path
        self._library = library
        self._list_updates_timeout = list_updates_timeout
        self._size_cache_ttl = size_cache_ttl
        self._info_timeout = info_timeout
        self._size_cache: dict[str, tuple[Any, ...]] = {}

    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        if not self._cli_path:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "list-installed",
                "--check-updates",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._list_updates_timeout,
            )
        except (TimeoutError, OSError) as e:
            logger.warning("[epic_updates] list-installed failed: %s", e)
            return []
        if proc.returncode != 0:
            return []
        return self._parse_update_output(
            stdout.decode(errors="ignore"),
        )

    @staticmethod
    def _parse_update_output(text: str) -> list[str]:
        """Parse update output."""
        updates: list[str] = []
        current_app: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("*") and "App name:" in stripped:
                try:
                    current_app = stripped.split("App name:")[1].split("|")[0].strip()
                except IndexError:
                    current_app = None
            elif stripped.startswith("-> Update available!") and current_app:
                updates.append(current_app)
                current_app = None
        return updates

    async def update_game(self, game_id: str, installer: Any, progress_cb: Any = None) -> InstallResult:
        """Update game."""
        if not self._cli_path:
            return InstallResult(
                success=False,
                error="legendary_not_found",
                store="epic",
                game_id=game_id,
            )
        installed = await self._library.read_installed_map()
        entry = installed.get(game_id)
        if not entry:
            return InstallResult(
                success=False,
                error="not_installed",
                store="epic",
                game_id=game_id,
            )
        # legendary records ``install_path`` at the TOP level of each
        # ``installed.json`` entry — there is no nested ``install`` object.
        # Reading only the nested key made ``base_path`` always ``None``, so
        # the installer fell back to the default internal root and logged a
        # path the update was never going to use. (legendary itself reuses
        # the existing install directory for an already-installed game and
        # ignores ``--base-path``, which is why nothing was mislocated — but
        # the read was still wrong.) ``library.py`` already reads it this way.
        current_path = (
            entry.get("install_path")
            or (entry.get("install") or {}).get("install_path", "")
        )
        base_path = str(Path(current_path).parent) if current_path else None
        result = await installer.install_game(
            game_id,
            base_path=base_path,
            progress_cb=progress_cb,
        )
        if result.success:
            self._size_cache.pop(game_id, None)
            self._library.invalidate_installed_cache()
        return cast("InstallResult", result)

    async def get_game_size(self, game_id: str) -> int | None:
        """Get game size."""
        if not self._cli_path:
            return None
        entry = self._size_cache.get(game_id)
        if entry is not None:
            size, ts = entry
            if time.time() - ts < self._size_cache_ttl:
                return cast("int | None", size)
        size = await self._load_game_size_from_cli(game_id)
        if size is not None:
            self._size_cache[game_id] = (size, time.time())
        return size

    async def _load_game_size_from_cli(self, game_id: str) -> int | None:
        """Load game size from cli."""
        info = await self._fetch_info(game_id)
        if info is None:
            return None
        manifest = info.get("manifest") or {}
        size = manifest.get("download_size")
        if not isinstance(size, int):
            return None
        return size

    async def _fetch_info(self, game_id: str) -> dict[str, Any] | None:
        """Fetch info."""
        if self._cli_path is None:
            return None
        return await fetch_info(
            self._cli_path,
            game_id,
            timeout=self._info_timeout,
            log_prefix="[epic_updates]",
        )
