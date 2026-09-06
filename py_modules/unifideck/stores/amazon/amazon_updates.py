"""Amazon Games update checker — periodic polling for new versions.

``AmazonUpdateChecker`` periodically queries nile for the latest
version manifest of each installed game and compares it against the
locally-recorded version (stored in the ``.unifideck-id`` marker).

* ``check_for_updates()``         — return a list of available updates;
* ``has_update(game_id)``         — single-game query;
* ``set_check_interval(seconds)`` — adjust the polling frequency;
* ``stop()``                      — graceful shutdown.

Update application itself is delegated to the installer pipeline
(``amazon_install.py``) which re-runs nile in update mode.
Rate-limiting protects against hammering the Amazon API on initial
library boot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from unifideck.core.binaries import clean_cli_env
from unifideck.event_bus.event_bus import EventBus
from unifideck.stores.amazon.nile_lock import nile_cli_lock

from .amazon_library import AmazonLibraryReader

logger = logging.getLogger(__name__)

class AmazonUpdateChecker:
    """Amazon update checker."""

    def __init__(
        self,
        bus: EventBus,
        cli_path: str | None,
        library: AmazonLibraryReader,
        list_updates_timeout: int,
        get_size_timeout: int,
        default_install_root: str,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._cli_path = cli_path
        self._library = library
        self._list_updates_timeout = list_updates_timeout
        self._get_size_timeout = get_size_timeout
        self._default_install_root = str(
            Path(default_install_root).expanduser(),
        )

    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        if not self._cli_path:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "list-updates",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._list_updates_timeout,
            )
        except (TimeoutError, OSError) as e:
            logger.warning(
                "[amazon_updates] list-updates failed: %s",
                e,
            )
            return []
        if proc.returncode != 0:
            return []
        try:
            raw = stdout.decode(errors="ignore").strip() or "[]"
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, str)]

    async def get_game_size(self, game_id: str) -> int | None:
        """Download size in bytes for ``game_id``, or ``None``.

        Serialised behind :func:`nile_cli_lock`: nile refreshes its token
        opportunistically and rewrites ``user.json`` non-atomically, so two
        of these running together can corrupt that file and log the user out
        of Amazon entirely.
        """
        if not self._cli_path:
            return None
        async with nile_cli_lock():
            stdout = await self._run_size_probe(game_id)
        if stdout is None:
            return None
        return self._parse_size(stdout)

    async def _run_size_probe(self, game_id: str) -> bytes | None:
        """Run ``nile install <id> --info --json``; stdout or ``None``."""
        assert self._cli_path is not None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "install",
                game_id,
                "--info",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._get_size_timeout,
            )
        except (TimeoutError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return stdout

    @staticmethod
    def _parse_size(stdout: bytes) -> int | None:
        """Pull ``download_size`` out of nile's JSON lines."""
        for raw_line in stdout.decode(errors="ignore").splitlines():
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue
            size = info.get("download_size")
            if isinstance(size, int):
                return size
        return None

    async def resolve_current_base_path(self, game_id: str) -> str:
        """Resolve current base path."""
        installed = await self._library.read_installed_ids()
        info = installed.get(game_id)
        if info and info.get("path"):
            return str(Path(info["path"]).parent) or self._default_install_root
        return self._default_install_root
