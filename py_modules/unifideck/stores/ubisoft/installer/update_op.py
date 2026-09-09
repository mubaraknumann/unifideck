"""
Game update operation — re-runs the installer in "update" mode.

When UPC publishes a new version of an installed game, the update is
applied by re-running the installer with a flag that tells UPC to
update-in-place rather than fresh-install. This module exposes
``UbisoftUpdateOp`` which wraps the install pipeline for the update case:
same orchestration as a fresh install, but skips the "create install
directory" phase and reuses the existing prefix.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from unifideck.core.types import InstallResult

from .launch_env import UpcLaunchEnvBuildError, _UpcLaunchEnv

if TYPE_CHECKING:
    from collections.abc import Callable

    from unifideck.stores.ubisoft.id_map import UbisoftIdMap
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
    from unifideck.stores.ubisoft.session import UbisoftSession
_UPDATE_TIMEOUT_S = 4 * 60 * 60
logger = logging.getLogger(__name__)

class _UpdateOperation:
    """Update operation."""

    def __init__(
        self,
        *,
        id_map: UbisoftIdMap,
        paths: UbisoftPrefixPaths,
        session: UbisoftSession,
        build_launch_env: Callable[..., _UpcLaunchEnv],
    ) -> None:
        """Initialize the instance."""
        self._id_map = id_map
        self._paths = paths
        self._session = session
        self._build_launch_env = build_launch_env

    async def update(self, game_id: str) -> InstallResult:
        """Update."""
        try:
            prepared = self._prepare_launch(game_id)
        except UpcLaunchEnvBuildError as e:
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error=e.error_code,
            )
        if isinstance(prepared, InstallResult):
            return prepared
        try:
            return await self._run_update_process(
                game_id,
                prepared,
            )
        except Exception as e:
            logger.exception("[UbisoftInstaller] update error for %s", game_id)
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error=f"update_exception: {e}",
            )

    def _prepare_launch(
        self,
        game_id: str,
    ) -> _UpcLaunchEnv | InstallResult:
        """Prepare launch."""
        prefix_path = self._paths.get_prefix_path(game_id)
        self._session.inject_into_prefix(prefix_path)
        launch_id = self._id_map.resolve_launch_id(game_id)
        if not launch_id:
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error="launch_id_not_resolved",
            )
        return self._build_launch_env(
            game_id,
            prefix_path,
            upc_missing_error=("upc_exe_not_found_reinstall_required"),
        )

    async def _run_update_process(
        self,
        game_id: str,
        launch_env: _UpcLaunchEnv,
    ) -> InstallResult:
        """Run update process."""
        launch_id = self._id_map.resolve_launch_id(game_id)
        launch_url = f"uplay://launch/{launch_id}/0"
        logger.info(
            "[UbisoftInstaller] triggering update via %s",
            launch_url,
        )
        proc = await asyncio.create_subprocess_exec(
            launch_env.python_bin,
            launch_env.umu_run,
            launch_env.upc_path,
            launch_url,
            env=launch_env.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=_UPDATE_TIMEOUT_S,
            )
        except TimeoutError:
            proc.kill()
            logger.warning(
                "[UbisoftInstaller] update timed out for %s",
                game_id,
            )
        return InstallResult(
            success=True,
            store="ubisoft",
            game_id=game_id,
        )
