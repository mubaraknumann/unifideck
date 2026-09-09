"""
UPC game uninstall pipeline — removes a game cleanly from the prefix.

``UbisoftUninstaller`` removes an installed Ubisoft game from disk and
from every state-tracking layer:

* the game's install directory (recursive remove);
* the entry in the installer registry;
* the entry in the id_map;
* the Steam shortcut (delegated to the shortcut service);
* the SteamGridDB artwork cache.

Operates idempotently: if any one of the layers has already been
removed, the corresponding cleanup is a no-op rather than a failure.
This is important because Unifideck can be re-installed and re-detect
half-removed state from a previous uninstall.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.core.types import Result

from . import registry as _reg
from .launch_env import UpcLaunchEnvBuildError

if TYPE_CHECKING:
    from .installer import UbisoftInstaller
logger = logging.getLogger(__name__)
_PROTOCOL_UNINSTALL_TIMEOUT_S = 60.0
_DELETE_MIN_PATH_DEPTH = 4

class _UninstallPipeline:
    """Uninstall pipeline."""

    def __init__(self, parent: UbisoftInstaller) -> None:
        """Initialize the instance."""
        self._parent = parent

    async def uninstall_game(
        self,
        game_id: str,
        *,
        delete_prefix: bool = False,
    ) -> Result:
        """Uninstall game.

        Thin wrapper: logs the request and turns any unexpected exception from
        the pipeline into a failed :class:`Result`. The actual sequence lives
        in :meth:`_perform_uninstall`.
        """
        try:
            logger.info(
                "[UbisoftInstaller] uninstalling %s (delete_prefix=%s)",
                game_id,
                delete_prefix,
            )
            return await self._perform_uninstall(
                game_id,
                delete_prefix=delete_prefix,
            )
        except Exception as e:
            logger.exception("[UbisoftInstaller] uninstall error for %s", game_id)
            return Result(
                success=False,
                error=f"uninstall_exception: {e}",
            )

    async def _perform_uninstall(
        self,
        game_id: str,
        *,
        delete_prefix: bool,
    ) -> Result:
        """Resolve targets, uninstall (direct or via UPC), and clean up.

        Raises on unexpected errors; :meth:`uninstall_game` wraps this in the
        top-level ``try/except``.
        """
        prefix_path, install_id, install_path = self.resolve_uninstall_targets(
            game_id
        )
        # Prefer deleting the game files ourselves over UPC's
        # ``uplay://uninstall`` protocol. Running UPC rotates the Ubisoft
        # refresh token (the server invalidates the old one) and the
        # uninstall path can't reliably capture the rotated token back —
        # so the shared login goes stale and the next install opens
        # signed-out (the "auth lost after uninstall" bug). When we can
        # locate the install directory from our own records (the
        # ``.unifideck_ubisoft`` install marker / detection cascade / the
        # UPC registry ``InstallDir``), delete it directly and never touch
        # UPC. Only fall back to the protocol uninstall when the files
        # can't be located — and capture the rotated token back so even
        # that path keeps the shared login current. ``capture()`` is
        # guarded (valid + newer + non-smaller), so a logout / half-write
        # can't poison auth/template.
        protocol_attempted = False
        if not await self._can_delete_directly(install_path, delete_prefix):
            protocol_attempted = await self.attempt_protocol_uninstall(
                game_id,
                prefix_path,
                install_id,
                delete_prefix,
            )
            install_path = self.refresh_install_path(
                game_id,
                prefix_path,
                install_path,
            )
        # Capture the prefix's CURRENT token back to the auth prefix before
        # we delete anything. UPC rotates the token on every run — including
        # PLAY, whose launcher subprocess never captures it back — so the
        # game prefix routinely holds a NEWER, still-valid token than auth
        # (auth is left on the pre-play token the rotation invalidated
        # server-side). Deleting the prefix without capturing strands auth
        # on that stale token → the next install opens signed-out. Runs
        # whether we direct-deleted or fell back to UPC (captures the latest
        # either way). ``_capture_rotated_session`` → ``capture()`` is
        # guarded (auth-only, skips a logged-out/smaller source), so it
        # never propagates a logout.
        self._capture_rotated_session(prefix_path)
        game_dir_error = await self.delete_game_directory(
            install_path,
            prefix_path,
            delete_prefix,
        )
        if game_dir_error:
            return Result(success=False, error=game_dir_error)
        prefix_deleted, prefix_error = await self.delete_prefix_if_requested(
            prefix_path,
            delete_prefix,
        )
        if prefix_error:
            return Result(success=False, error=prefix_error)
        self.post_uninstall_cleanup(
            game_id,
            prefix_path,
            install_id,
            prefix_deleted,
        )
        logger.info(
            "[UbisoftInstaller] game %s uninstalled "
            "(protocol_attempted=%s, prefix_deleted=%s)",
            game_id,
            protocol_attempted,
            prefix_deleted,
        )
        return Result(success=True)

    async def _can_delete_directly(
        self,
        install_path: str | None,
        delete_prefix: bool,
    ) -> bool:
        """Whether we can uninstall by deleting files ourselves (no UPC).

        True when the whole prefix is being deleted (that path already skips
        UPC), or when ``resolve_uninstall_targets`` located a concrete game
        directory on disk (from the install marker / detection cascade / UPC
        registry ``InstallDir``). When the files can't be located — e.g. a
        game installed to an arbitrary path outside any known root — this
        returns False and the caller falls back to ``uplay://uninstall`` so
        UPC removes the files it placed.
        """
        if delete_prefix:
            return True
        if not install_path:
            return False
        return await asyncio.to_thread(lambda: Path(install_path).is_dir())

    def _capture_rotated_session(self, prefix_path: str) -> None:
        """Capture the prefix's current UPC token back to auth before delete.

        Runs on every uninstall (direct-delete or UPC-fallback) right before
        the prefix is removed. UPC rotates the Ubisoft refresh token on every
        run — install AND play — and the Play launcher subprocess never
        captures it back, so the game prefix usually holds a newer, still-valid
        token than the auth prefix. Deleting the prefix without capturing that
        token strands auth on a server-stale token and the next install opens
        signed-out. ``capture()`` is guarded (auth-only, skips a logged-out /
        smaller source), so a logout / half-write is never propagated.
        Best-effort — a failure here never blocks the uninstall.
        """
        try:
            if self._parent._session.capture(prefix_path):
                self._parent._session.propagate_all_to_all()
                logger.info(
                    "[UbisoftInstaller] captured UPC token from prefix before "
                    "uninstall → auth refreshed",
                )
        except Exception as e:
            logger.warning(
                "[UbisoftInstaller] uninstall session capture failed: %s",
                e,
            )

    def resolve_uninstall_targets(
        self,
        game_id: str,
    ) -> tuple[str, str | None, str | None]:
        """Resolve uninstall targets."""
        prefix_path = self._parent._paths.get_prefix_path(
            game_id,
        )
        game_info = self._parent._library._detector._detect_installed_game(
            game_id, prefix_path
        )
        install_path = game_info.get("install_path") if game_info else None
        install_id = self._parent._id_map.resolve_install_id(
            game_id
        ) or self._parent._id_map.resolve_launch_id(game_id)
        return prefix_path, install_id, install_path

    async def attempt_protocol_uninstall(
        self,
        game_id: str,
        prefix_path: str,
        install_id: str | None,
        delete_prefix: bool,
    ) -> bool:
        """Attempt protocol uninstall."""
        if delete_prefix:
            logger.info(
                "[UbisoftInstaller] delete_prefix=True: "
                "skipping uninstall URI, deleting files "
                "directly",
            )
            return False
        if not install_id:
            return False
        return await self.try_protocol_uninstall(
            game_id,
            prefix_path,
            install_id,
        )

    def refresh_install_path(
        self,
        game_id: str,
        prefix_path: str,
        install_path: str | None,
    ) -> str | None:
        """Refresh install path."""
        post_info = self._parent._library._detector._detect_installed_game(
            game_id, prefix_path
        )
        if post_info:
            return post_info.get("install_path") or install_path
        return install_path

    async def delete_game_directory(
        self,
        install_path: str | None,
        prefix_path: str,
        delete_prefix: bool,
    ) -> str | None:
        """Delete game directory."""
        if not install_path or not await asyncio.to_thread(lambda: Path(install_path).is_dir()):
            return None
        inside_prefix = str(
            await asyncio.to_thread(lambda: Path(install_path).resolve()),
        ).startswith(
            str(await asyncio.to_thread(lambda: Path(prefix_path).resolve())) + "/",
        )
        if inside_prefix and delete_prefix:
            return None
        logger.info(
            "[UbisoftInstaller] fallback deleting game directory: %s",
            install_path,
        )
        deleted = await self.delete_tree_with_retries(
            install_path,
            "Ubisoft game install directory",
        )
        if not deleted:
            return f"game_dir_delete_failed: {install_path}"
        return None

    async def delete_prefix_if_requested(
        self,
        prefix_path: str,
        delete_prefix: bool,
    ) -> tuple[bool, str | None]:
        """Delete prefix if requested."""
        if not delete_prefix:
            return False, None
        if not await asyncio.to_thread(lambda: Path(prefix_path).is_dir()):
            return False, None
        deleted = await self.delete_tree_with_retries(
            prefix_path,
            "Ubisoft game prefix",
        )
        if not deleted:
            return False, f"prefix_delete_failed: {prefix_path}"
        return True, None

    def post_uninstall_cleanup(
        self,
        game_id: str,
        prefix_path: str,
        install_id: str | None,
        prefix_deleted: bool,
    ) -> None:
        """Post uninstall cleanup."""
        if not prefix_deleted:
            _reg.clean_install_registry(
                prefix_path,
                install_id or "",
            )
            return
        if self._parent._id_map.in_cache(game_id):
            self._parent._id_map._cache.pop(game_id, None)
            self._parent._id_map._save()

    async def try_protocol_uninstall(
        self,
        game_id: str,
        prefix_path: str,
        install_id: str,
    ) -> bool:
        """Try protocol uninstall."""
        try:
            launch_env = self._parent._build_upc_launch_env(
                game_id,
                prefix_path,
            )
        except UpcLaunchEnvBuildError:
            return False
        upc_path = launch_env.upc_path
        umu_run = launch_env.umu_run
        python_bin = launch_env.python_bin
        env = launch_env.env
        uninstall_url = f"uplay://uninstall/{install_id}"
        logger.info(
            "[UbisoftInstaller] trying protocol uninstall: %s",
            uninstall_url,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin,
                umu_run,
                upc_path,
                uninstall_url,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=_PROTOCOL_UNINSTALL_TIMEOUT_S,
                )
            except TimeoutError:
                proc.kill()
                logger.warning(
                    "[UbisoftInstaller] protocol uninstall "
                    "timed out, falling back to direct "
                    "delete",
                )
            return True
        except OSError as e:
            logger.warning(
                "[UbisoftInstaller] protocol uninstall spawn failed: %s",
                e,
            )
            return False

    def _is_path_safe_to_delete(
        self,
        target_path: str,
        label: str,
    ) -> bool:
        """Is path safe to delete."""
        if not target_path:
            logger.error(
                "[UbisoftInstaller] refusing to delete empty path for %s",
                label,
            )
            return False
        resolved = str(Path(target_path).resolve())
        home_dir = str(Path("~").expanduser().resolve())
        config = self._parent._config
        protected = {
            "/",
            home_dir,
            str(Path(config.data_dir_expanded).resolve()),
            str(Path(config.prefixes_dir_expanded).resolve()),
            str(
                Path(
                    config.default_install_base_expanded,
                ).resolve(),
            ),
            str(Path(config.sdcard_install_base).resolve()),
        }
        if resolved in protected or len(resolved.strip("/")) < _DELETE_MIN_PATH_DEPTH:
            logger.error(
                "[UbisoftInstaller] refusing to delete unsafe path for %s: %s",
                label,
                resolved,
            )
            return False
        return True

    async def delete_tree_with_retries(
        self,
        target_path: str,
        label: str,
        *,
        retries: int = 3,
    ) -> bool:
        """Delete tree with retries."""
        if not self._is_path_safe_to_delete(target_path, label):
            return False
        resolved = str(await asyncio.to_thread(lambda: Path(target_path).resolve()))
        if not await asyncio.to_thread(lambda: Path(resolved).is_dir()):
            logger.info(
                "[UbisoftInstaller] nothing to delete for %s: %s",
                label,
                resolved,
            )
            return True
        for attempt in range(1, retries + 1):
            try:
                shutil.rmtree(resolved)
                logger.info(
                    "[UbisoftInstaller] deleted %s: %s",
                    label,
                    resolved,
                )
                return True
            except OSError as e:
                logger.warning(
                    "[UbisoftInstaller] delete attempt %d/%d failed for %s: %s",
                    attempt,
                    retries,
                    label,
                    e,
                )
                if attempt < retries:
                    await asyncio.sleep(1.5)
        logger.error(
            "[UbisoftInstaller] delete failed after %d attempts for %s: %s",
            retries,
            label,
            resolved,
        )
        return False
