"""
UPC installer orchestration — drives the multi-phase install pipeline.

``UbisoftInstaller`` orchestrates a full UPC install through the
following phases:

1. **bootstrap UPC** — download the UbisoftConnectInstaller.exe from the
   Ubisoft CDN if absent, store in ``installer_cache_dir``;
2. **launch UPC headlessly** — wine-run the installer with the right env;
3. **drive the manual UI** — UPC has no silent-install switch, so the
   manual_ui module operates UPC visually via window-detection;
4. **register the install** — write Unifideck's marker + update the id_map.

Errors at any phase are wrapped into an ``InstallResult`` envelope; the
phase is identified in the error code so the UI can report exactly
which step failed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from unifideck.core.types import InstallResult, Result
from unifideck.stores.shared.prefix_forensics import (
    preserve_vendor_logs,
    salvage_path,
)
from unifideck.stores.shared.prefix_placement import (
    cleanup_abandoned_prefix,
    reset_for_fresh_install,
    resolve_prefix_target,
)
from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.library import UbisoftLibrary
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.session import UbisoftSession

from . import registry as _reg
from .launch_env import (
    UpcLaunchEnvBuildError,
    _UpcLaunchEnv,
)
from .launcher import _LauncherInstall
from .manual_ui import _ManualUiInstaller
from .registry import _ShortcutRegistry
from .uninstall import _UninstallPipeline
from .update_op import _UpdateOperation

logger = logging.getLogger(__name__)
_UPDATE_TIMEOUT_S = 4 * 60 * 60

class UbisoftInstaller:
    """Ubisoft installer."""

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        binaries: UbisoftBinaryResolver,
        id_map: UbisoftIdMap,
        session: UbisoftSession,
        library: UbisoftLibrary,
        bootstrap_game_prefix: Callable[
            [str],
            Awaitable[bool],
        ],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._binaries = binaries
        self._id_map = id_map
        self._session = session
        self._library = library
        self._bootstrap_game_prefix = bootstrap_game_prefix
        self._shortcut_registry = _ShortcutRegistry(config)
        self._active_install_pids: dict[str, int] = {}
        self._manual_ui_installer = _ManualUiInstaller(
            config=config,
            library=library,
            id_map=id_map,
            session=session,
            active_install_pids=self._active_install_pids,
        )
        self._uninstall_pipeline = _UninstallPipeline(self)
        self._launcher = _LauncherInstall(self)
        self._update_op = _UpdateOperation(
            id_map=id_map,
            paths=paths,
            session=session,
            build_launch_env=self._build_upc_launch_env,
        )

    async def uninstall_game(
        self,
        game_id: str,
        *,
        delete_prefix: bool = False,
    ) -> Result:
        """Uninstall game."""
        return await self._uninstall_pipeline.uninstall_game(
            game_id,
            delete_prefix=delete_prefix,
        )

    async def open_launcher_for_install(
        self,
        game_id: str,
    ) -> Result:
        """Open launcher for install."""
        return await self._launcher.open_launcher_for_install(
            game_id,
        )

    def _build_upc_launch_env(
        self,
        game_id: str,
        prefix_path: str,
        *,
        prefer_connect_exe: bool = False,
        upc_missing_error: str = "upc_exe_not_found",
    ) -> _UpcLaunchEnv:
        """Build UPC launch env."""
        upc_path: str | None = None
        if prefer_connect_exe:
            upc_path = self._paths.find_connect_exe(prefix_path)
        if not upc_path:
            upc_path = self._paths.find_upc_exe(prefix_path)
        if not upc_path:
            raise UpcLaunchEnvBuildError(upc_missing_error)
        umu_run = self._binaries.find_umu_run()
        if not umu_run:
            raise UpcLaunchEnvBuildError("umu_run_not_found")
        python_bin = self._binaries.find_python()
        env = self._binaries.build_umu_env(
            wineprefix=prefix_path,
            gameid=f"umu-ubisoft-{game_id}",
            store_game_id=f"ubisoft:{game_id}",
            steam_window_env=self._build_steam_window_env(
                f"ubisoft:{game_id}",
            ),
        )
        return _UpcLaunchEnv(
            upc_path=upc_path,
            umu_run=umu_run,
            python_bin=python_bin,
            env=env,
        )

    async def install_game(
        self,
        game_id: str,
        *,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        install_path: str | None = None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> InstallResult:
        """Install game.

        UPC is no longer spawned from here — in Gaming Mode a backend
        subprocess has no gamescope session, so the window never appears.
        Instead we bootstrap the per-game prefix, then invoke ``on_ready``
        (the worker emits a frontend RunGame request from it) and monitor
        the prefix for the installed files. ``on_ready`` fires only after
        the prefix (and its UPC install) is ready, so RunGame opens UPC
        into a prepared prefix.
        """
        logger.info("[UbisoftInstall] install_game game_id=%s install_path=%s", game_id, install_path)
        try:
            logger.info(
                "[UbisoftInstaller] installing game %s",
                game_id,
            )
            # Every Install starts from a CLEAN prefix so an abandoned install
            # never leaves an orphaned prefix eating disk. Delete any prior
            # per-game prefix — both the previously recorded location (which
            # can differ when the user picks a new disk, or linger from a prior
            # uninstall) and the resolved target — then bootstrap builds fresh
            # (no reuse). Play never reaches this path, so it never resets a
            # prefix; and the button is "Installing" during an active install,
            # so this only runs when no install is in flight.
            new_prefix = str(
                resolve_prefix_target(
                    "ubisoft",
                    game_id,
                    install_path,
                    self._paths.get_prefix_path(game_id),
                ),
            )
            old_prefix = self._id_map.resolve_prefix_path(game_id)
            await self._reset_prefix_for_fresh_install(old_prefix, new_prefix)
            # Per-game prefix placement: the storage location the user picked
            # becomes the prefix root, so the game (which UPC installs into
            # the prefix's drive_c) lands on the chosen disk. Record it BEFORE
            # bootstrap so ``get_prefix_path`` — used by bootstrap, the
            # launcher, detection and uninstall — all resolve the same dir.
            if install_path:
                self._id_map.set_prefix_path(game_id, new_prefix)
            if not await self._bootstrap_game_prefix(game_id):
                return InstallResult(
                    success=False,
                    store="ubisoft",
                    game_id=game_id,
                    error="prefix_bootstrap_failed",
                )
            return await self._drive_upc_install(
                game_id,
                progress_cb=progress_cb,
                install_path=install_path,
                on_ready=on_ready,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("[UbisoftInstaller] install error for %s", game_id)
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error=f"install_exception: {e}",
            )

    async def _drive_upc_install(
        self,
        game_id: str,
        *,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        install_path: str | None = None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> InstallResult:
        """Run the UPC UI install for an already-bootstrapped prefix.

        Resolves the prefix + game name, builds the UPC launch env, then
        drives ``install_via_upc_ui``. A cancelled or failed install cleans
        up the prefix we created (cancellation is re-raised). Extracted from
        ``install_game`` to keep that method under the line cap.
        """
        prefix_path = self._paths.get_prefix_path(game_id)
        # Guarantee the prefix is actually populated with upc.exe BEFORE we
        # signal the frontend to RunGame UPC. A prior abandoned/cleaned
        # install can leave the resolved path an empty directory; firing
        # on_ready into an empty prefix makes the launcher exit immediately
        # (upc.exe not found) — the user sees a black flash and nothing
        # opens. Re-bootstrap (idempotent: reuses an existing populated
        # prefix, else clones the .template) and re-check; only proceed once
        # upc.exe is present so RunGame opens UPC into a prepared prefix.
        if not self._paths.find_upc_exe(prefix_path):
            logger.warning(
                "[UbisoftInstaller] resolved prefix %s for %s has no "
                "upc.exe — re-bootstrapping before launch",
                prefix_path,
                game_id,
            )
            await self._bootstrap_game_prefix(game_id)
            if not self._paths.find_upc_exe(prefix_path):
                logger.error(
                    "[UbisoftInstaller] prefix %s still missing upc.exe "
                    "after bootstrap — aborting install for %s "
                    "(no RunGame signal emitted)",
                    prefix_path,
                    game_id,
                )
                return InstallResult(
                    success=False,
                    store="ubisoft",
                    game_id=game_id,
                    error="upc_exe_not_found",
                )
        game_name = self._library._detector._get_game_name(game_id)
        try:
            # Called for its preconditions, not its value: UPC is opened by
            # the frontend now, so nothing here spawns it, but a missing
            # upc.exe or umu-run still has to fail the install with a specific
            # error code rather than surface later as a silent no-op launch.
            self._build_upc_launch_env(game_id, prefix_path)
        except UpcLaunchEnvBuildError as e:
            return InstallResult(
                success=False,
                store="ubisoft",
                game_id=game_id,
                error=e.error_code,
            )
        try:
            result = await self._manual_ui_installer.install_via_upc_ui(
                game_id=game_id,
                game_name=game_name,
                prefix_path=prefix_path,
                progress_cb=progress_cb,
                install_path=install_path,
                on_ready=on_ready,
            )
        except asyncio.CancelledError:
            # Cancelled from the download queue — clean up the prefix we
            # created if the game never landed, then propagate.
            await self._cleanup_abandoned_prefix(game_id, prefix_path)
            raise
        if not result.success:
            await self._cleanup_abandoned_prefix(game_id, prefix_path)
        return result

    async def _reset_prefix_for_fresh_install(
        self,
        old_prefix: str | None,
        new_prefix: str,
    ) -> None:
        """Delete any pre-existing per-game prefix(es) so Install starts clean.

        Policy lives in ``stores/shared/prefix_placement``; the subsequent
        ``set_prefix_path`` + ``bootstrap_game_prefix`` then build a fresh,
        auth-injected prefix with no reuse.
        """
        await reset_for_fresh_install(
            old_prefix,
            new_prefix,
            self._prefix_remover("fresh-install prefix reset"),
            label="UbisoftInstaller",
        )

    def _prefix_remover(self, reason: str) -> Callable[[Path], Awaitable[bool]]:
        """Ubisoft's guarded remover: protected paths + depth check + retries.

        Handed to the shared placement helpers so the *policy* is shared
        while the deletion stays behind this store's own backstop.
        """

        async def _remove(path: Path) -> bool:
            return await self._uninstall_pipeline.delete_tree_with_retries(
                str(path), reason,
            )

        return _remove

    async def _cleanup_abandoned_prefix(
        self,
        game_id: str,
        prefix_path: str,
    ) -> None:
        """Remove the prefix created for an install that produced no game.

        Triggered on a cancelled / failed Ubisoft install so abandoned
        prefixes don't accumulate at the user's chosen storage location.
        Gating (recorded locations only, never one holding a game) is shared
        in ``stores/shared/prefix_placement``; the guard below is ours.

        A bootstrapped-but-no-game prefix (upc.exe present, no game folder) is
        deleted — resume is intentionally not preserved: each Install rebuilds
        the prefix fresh, so keeping an abandoned UPC prefix would only orphan
        disk.

        UPC's own logs come out first. They live inside the prefix, so this
        deletion is otherwise the only thing standing between a failed install
        and the sole first-hand account of why it failed — the same reason
        Battle.net salvages here, and it has already cost one field
        investigation there.
        """
        await self._salvage_upc_logs(game_id, prefix_path)
        deleted = await cleanup_abandoned_prefix(
            prefix_path,
            recorded=self._id_map.resolve_prefix_path(game_id),
            holds_game=lambda path: self._prefix_holds_game(game_id, str(path)),
            remover=self._prefix_remover("abandoned Ubisoft prefix"),
            label="UbisoftInstaller",
        )
        if deleted:
            self._id_map.clear_prefix_path(game_id)

    @staticmethod
    async def _salvage_upc_logs(game_id: str, prefix_path: str) -> None:
        """Copy UPC's own logs out before the prefix goes.

        Best-effort by construction — :func:`preserve_vendor_logs` swallows
        everything and returns a count. A salvage must never be the reason a
        prefix the user is waiting on does not get reclaimed, so this stays
        ahead of the deletion but can never block it.
        """
        await preserve_vendor_logs(
            "ubisoft", Path(prefix_path), salvage_path("ubisoft", game_id),
        )

    def _prefix_holds_game(self, game_id: str, prefix_path: str) -> bool:
        """Whether this prefix holds a real game — double-guarded.

        The user explicitly warned against deleting a prefix that holds a
        game, so this answers True if EITHER the install detector finds one
        OR the UPC ``games/`` dir actually contains a game folder. The second guard
        matters because the snapshot-based detector can false-negative (it
        does not always notice a completed install), and we must never delete
        real game files.
        """
        game_info = self._library._detector._detect_installed_game(
            game_id, prefix_path,
        )
        return bool(
            (game_info and game_info.get("install_path"))
            or _reg.prefix_has_game_files(prefix_path),
        )

    def is_install_session_active(self, game_id: str) -> bool:
        """Check whether install session active."""
        pid = self._active_install_pids.get(game_id)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            self._active_install_pids.pop(game_id, None)
            return False

    async def cancel_install_session(
        self,
        game_id: str,
    ) -> Result:
        """Stop an in-progress UPC install and sync any captured creds.

        UPC is now launched by the frontend via RunGame (not a backend
        subprocess), so there's no PID to signal. ``pkill -f upc.exe``
        closes the launcher window regardless of which gamescope session
        it lives in — a process kill works cross-session where window
        probing does not. The actual queue-item teardown still happens in
        ``manual_ui.install_via_upc_ui``'s finally block (which also
        pkills); this is the store-level entry point kept for parity.
        """
        self._active_install_pids.pop(game_id, None)
        self.kill_upc_processes()
        prefix_path = self._paths.get_prefix_path(game_id)
        # mypy strict mis-resolves the asyncio.to_thread overload here
        # against the lambda's bool return — the actual signature is
        # ``Callable[..., T] -> Awaitable[T]`` and this works fine at
        # runtime. The combined arg-type + return-value pair is the
        # overload-resolution noise, not a real type error.
        if prefix_path and await asyncio.to_thread(
            lambda: Path(prefix_path).is_dir(),  # type: ignore[arg-type,return-value]
        ):
            await asyncio.sleep(2)
            captured = self._session.capture(prefix_path)
            if captured:
                self._session.propagate_all_to_all()
                logger.info(
                    "[UbisoftInstaller] post-cancel: propagated session from %s",
                    game_id,
                )
            else:
                logger.info(
                    "[UbisoftInstaller] post-cancel: credentials synced for %s",
                    game_id,
                )
        return Result(success=True)

    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        return []

    async def update_game(
        self,
        game_id: str,
    ) -> InstallResult:
        """Update game."""
        return await self._update_op.update(game_id)

    def inject_install_registry(
        self,
        prefix_path: str,
        install_id: str,
        install_dir: str,
    ) -> None:
        """Inject install registry."""
        _reg.inject_install_registry(
            prefix_path,
            install_id,
            install_dir,
        )

    def kill_upc_processes(self) -> None:
        """Kill UPC processes."""
        try:
            subprocess.run(
                ["pkill", "-f", "upc.exe"],
                capture_output=True,
                timeout=5,
                check=False,  # pkill rc=1 on "no match" is expected
            )
            logger.info(
                "[UbisoftInstaller] killed upc.exe processes",
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "[UbisoftInstaller] pkill upc.exe failed: %s",
                e,
            )

    def _build_steam_window_env(
        self,
        store_game_id: str | None,
    ) -> dict[str, str]:
        """Build steam window env.

        The encoding lives in :mod:`unifideck.steam.window_env` — the game
        launch path needs the same block, and it went years without one
        (games launched behind Steam's loading screen as a result), so there
        is exactly one implementation now.
        """
        from unifideck.steam.window_env import build_steam_window_env

        appid = self._shortcut_registry.resolve_shortcut_appid(
            store_game_id,
        )
        return build_steam_window_env(appid, log_tag="UbisoftInstaller")
