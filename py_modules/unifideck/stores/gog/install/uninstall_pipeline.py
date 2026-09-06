"""Uninstall pipeline orchestrator.

``_UninstallPipeline`` is the symmetric counterpart to ``GOGInstaller``:
removes a game from every state-tracking layer (install directory,
gogdl manifests, .unifideck-id marker, Steam shortcut, SteamGridDB
artwork cache).

Operates idempotently — if any layer's entry is already gone, the
corresponding cleanup is a no-op rather than a failure.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.core.types import Result

from .primitives import GOGFolderOps

if TYPE_CHECKING:
    from .installer import GOGInstaller
logger = logging.getLogger(__name__)
_UNINSTALL_MAX_ATTEMPTS = 3

class _UninstallPipeline:
    """Uninstall pipeline."""

    def __init__(self, parent: GOGInstaller) -> None:
        """Initialize the instance."""
        self._parent = parent

    async def uninstall_game(
        self,
        game_id: str,
        install_path: str | None = None,
    ) -> Result:
        """Uninstall game."""
        if not install_path or not await asyncio.to_thread(lambda: Path(install_path).exists()):
            logger.info(
                "[GOGInstaller] %s already gone, nothing to do",
                game_id,
            )
            return Result(success=True)
        for attempt in range(_UNINSTALL_MAX_ATTEMPTS):
            try:
                await asyncio.to_thread(
                    shutil.rmtree,
                    install_path,
                )
            except PermissionError as e:
                logger.warning(
                    "[GOGInstaller] attempt %d permission: %s",
                    attempt + 1,
                    e,
                )
            except OSError as e:
                logger.warning(
                    "[GOGInstaller] attempt %d failed: %s",
                    attempt + 1,
                    e,
                )
            if not await asyncio.to_thread(lambda: Path(install_path).exists()):
                logger.info(
                    "[GOGInstaller] uninstalled %s",
                    install_path,
                )
                break
            remaining = GOGFolderOps.count_files(install_path)
            logger.warning(
                "[GOGInstaller] attempt %d: %d files remain",
                attempt + 1,
                remaining,
            )
            if attempt == _UNINSTALL_MAX_ATTEMPTS - 1:
                logger.info(
                    "[GOGInstaller] falling back to force cleanup",
                )
                await GOGFolderOps.force_cleanup_folder(
                    install_path,
                )
        await self._parent._wipe_support_cache(game_id)
        await self._parent._wipe_manifests(game_id)
        if await asyncio.to_thread(lambda: Path(install_path).exists()):
            remaining = GOGFolderOps.count_files(install_path)
            if remaining > 0:
                return Result(
                    success=False,
                    error=(f"uninstall_incomplete_{remaining}_remaining"),
                )
        return Result(success=True)
