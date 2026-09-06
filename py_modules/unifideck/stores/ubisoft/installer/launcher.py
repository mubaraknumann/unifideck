"""
UPC installer subprocess launcher — wine-runs UbisoftConnectInstaller.exe.

``UbisoftInstallerLauncher`` wraps the ``proton run`` / ``wine`` call
that launches the UPC installer executable inside a dedicated prefix.
It handles:

* env-var composition (delegated to ``launch_env.py``);
* working-directory setup (must be the directory containing the .exe);
* subprocess lifetime (Popen + watchdog timeout);
* exit-code interpretation (UPC uses non-standard codes for some
  scenarios — "already installed", "license accepted", etc.).

Returns a ``Result`` with the launch outcome; the manual UI driver
(``manual_ui.py``) takes over once the launcher reports success.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Result

from .launch_env import UpcLaunchEnvBuildError

if TYPE_CHECKING:
    from .installer import UbisoftInstaller
logger = logging.getLogger(__name__)

class _LauncherInstall:
    """Launcher install."""

    def __init__(self, parent: UbisoftInstaller) -> None:
        """Initialize the instance."""
        self._parent = parent
        # Strong references to the post-spawn monitor tasks so they
        # aren't garbage-collected while the UPC installer is still
        # running. Each task removes itself on completion.
        self._monitor_tasks: set[asyncio.Task[Any]] = set()

    async def open_launcher_for_install(
        self,
        game_id: str,
    ) -> Result:
        """Open launcher for install."""
        try:
            logger.info(
                "[UbisoftInstaller] open_launcher_for_install for %s",
                game_id,
            )
            if not await self._parent._bootstrap_game_prefix(game_id):
                return Result(
                    success=False,
                    error="prefix_bootstrap_failed",
                )
            prefix_path = self._parent._paths.get_prefix_path(
                game_id,
            )
            try:
                launch_env = self._parent._build_upc_launch_env(
                    game_id,
                    prefix_path,
                    prefer_connect_exe=True,
                    upc_missing_error="ubisoft_connect_not_found",
                )
            except UpcLaunchEnvBuildError as e:
                return Result(success=False, error=e.error_code)
            self._parent._session.inject_into_prefix(prefix_path)
            launch_id = self._parent._id_map.resolve_launch_id(
                game_id,
            )
            launch_url = f"uplay://install/{launch_id}" if launch_id else ""
            cmd = [
                launch_env.python_bin,
                launch_env.umu_run,
                launch_env.upc_path,
            ]
            if launch_url:
                cmd.append(launch_url)
            return await self._spawn_and_monitor_upc(
                cmd,
                launch_env.env,
                game_id,
                prefix_path,
            )
        except Exception as e:
            logger.exception("[UbisoftInstaller] launcher spawn failed for %s", game_id)
            return Result(
                success=False,
                error=f"launcher_spawn_exception: {e}",
            )

    async def _spawn_and_monitor_upc(
        self,
        cmd: list[str],
        env: dict[str, str],
        game_id: str,
        prefix_path: str,
    ) -> Result:
        """Spawn and monitor UPC."""
        logger.info(
            "[UbisoftInstaller] launch cmd: %s",
            " ".join(cmd),
        )
        logger.info(
            "[UbisoftInstaller] WINEPREFIX=%s PROTONPATH=%s GAMEID=%s",
            env.get("WINEPREFIX"),
            env.get("PROTONPATH"),
            env.get("GAMEID"),
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(
            "[UbisoftInstaller] spawned PID=%d",
            proc.pid,
        )
        self._parent._active_install_pids[game_id] = proc.pid
        spawned_pid = proc.pid
        monitor_task = asyncio.create_task(
            self.monitor_after_exit(
                game_id,
                spawned_pid,
                proc,
                prefix_path,
            ),
        )
        self._monitor_tasks.add(monitor_task)
        monitor_task.add_done_callback(self._monitor_tasks.discard)
        await asyncio.sleep(2)
        if proc.returncode is None:
            return Result(success=True)
        logger.error(
            "[UbisoftInstaller] UPC exited immediately (rc=%d)",
            proc.returncode,
        )
        if self._parent._active_install_pids.get(game_id) == spawned_pid:
            self._parent._active_install_pids.pop(game_id, None)
        return Result(
            success=False,
            error=f"ubisoft_connect_exited_code_{proc.returncode}",
        )

    async def monitor_after_exit(
        self,
        game_id: str,
        spawned_pid: int,
        proc: asyncio.subprocess.Process,
        prefix_path: str,
    ) -> None:
        """Monitor after exit."""
        try:
            _stdout, stderr = await proc.communicate()
            rc = proc.returncode
            logger.info(
                "[UbisoftInstaller] UPC exited (PID=%d, rc=%s)",
                spawned_pid,
                rc,
            )
            if stderr:
                stderr_text = stderr.decode(
                    errors="replace",
                )[:2000]
                logger.info(
                    "[UbisoftInstaller] UPC stderr: %s",
                    stderr_text,
                )
        except Exception as e:
            logger.warning(
                "[UbisoftInstaller] monitor error: %s",
                e,
            )
        finally:
            if self._parent._active_install_pids.get(game_id) == spawned_pid:
                self._parent._active_install_pids.pop(
                    game_id,
                    None,
                )
                captured = self._parent._session.capture(prefix_path)
                if captured:
                    self._parent._session.propagate_all_to_all()
