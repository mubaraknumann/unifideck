"""Epic Games launchable .exe resolver — handle multi-binary installs.

Epic-installed games often contain multiple .exe files (the game, a
crash reporter, an Easy Anti-Cheat launcher, a redistributable
installer). ``EpicExeResolver`` implements the heuristics to pick
the right one to launch :

1. **manifest match** — read legendary's manifest, use the
   ``launchExecutable`` field if present;
2. **subdirectory probe** — many Epic titles install under
   ``<game>/<EngineVersion>/<Game>/Binaries/Win64/<Game>.exe``,
   walk this canonical layout;
3. **filename heuristic** — prefer "<game>-Win64-Shipping.exe"
   over crash reporters / EAC launchers / redistributables;
4. **size filter** — exclude .exe files below 1 MiB (almost always
   tools, not the game).

Returns ``str`` paths to maintain compatibility with subprocess
callers that pass paths directly to ``proton run``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .legendary import fetch_info

logger = logging.getLogger(__name__)

class EpicExeResolver:
    """Epic exe resolver."""

    def __init__(
        self,
        cli_path: str | None,
        find_exe: Callable[[str, list[str] | None], str | None],
        info_timeout_seconds: float,
    ) -> None:
        """Initialize the instance."""
        self._cli_path = cli_path
        self._find_exe = find_exe
        self._info_timeout = info_timeout_seconds

    async def resolve(self, game_id: str) -> dict[str, Any]:
        """Resolve."""
        info = await self._fetch_info(game_id)
        install_path = self._extract_install_path(info)
        exe = self._resolve_executable(install_path, info, game_id)
        title = self._extract_title(info, game_id)
        return {
            "install_path": install_path,
            "executable": exe,
            "title": title,
        }

    async def _fetch_info(self, game_id: str) -> dict[str, Any] | None:
        """Fetch info."""
        if self._cli_path is None:
            return None
        return await fetch_info(
            self._cli_path,
            game_id,
            timeout=self._info_timeout,
            log_prefix="[epic_exe_resolver]",
        )

    @staticmethod
    def _extract_install_path(info: dict[str, Any] | None) -> str | None:
        """Extract install path."""
        if not info:
            return None
        install = info.get("install") or {}
        path = install.get("install_path")
        return path if isinstance(path, str) and path else None

    @staticmethod
    def _extract_title(info: dict[str, Any] | None, game_id: str) -> str:
        """Extract title."""
        if not info:
            return game_id
        game = info.get("game") or {}
        title = game.get("title")
        return title if isinstance(title, str) and title else game_id

    def _resolve_executable(self, install_path: str | None, info: dict[str, Any] | None, game_id: str) -> str | None:
        """Resolve executable."""
        if not install_path:
            return None
        if info is not None:
            manifest = info.get("manifest") or {}
            launch_exe = manifest.get("launch_exe")
            if isinstance(launch_exe, str) and launch_exe:
                cleaned = launch_exe.lstrip("/")
                candidate = str(Path(install_path) / cleaned)
                if Path(candidate).is_file():
                    return candidate
                logger.warning(
                    "[epic_exe_resolver] manifest launch_exe "
                    "missing on disk: %s → fallback to ExeFinder",
                    candidate,
                )
        return self._find_exe(install_path, [game_id])
