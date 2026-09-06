"""GameVault install pipeline — acquire an archive, extract it, register it.

One pipeline for both modes. Where the archive comes from, and whether it is
disposable afterwards, is the :class:`~.sources.ArchiveSource` the store
injected; everything here is shared:

    1. source.acquire()   → archive on local disk (download, or already there)
    2. extract            → install_root/<dir_name>/     (archive.py)
    3. find the exe       → best launch target            (exe_finder.py)
    4. write the marker   → the record that survives      (markers.py)
    5. source.release()   → delete the staged copy, or keep the user's file

Step 5 is the whole reason the pipeline needs no mode branch. It runs from a
``finally`` on every path, and the source decides what "release" means:
:class:`~.sources.RemoteArchiveSource` unlinks its staged download,
:class:`~.sources.LocalArchiveSource` does nothing, because that file is the
user's only copy. Deleting the wrong one is prevented by ownership, not by an
``if``.

**Known gap: an archive that is an installer.** A GameVault library holds
whatever its owner uploaded, and that is often a repack or an offline
installer rather than a ready-to-run game directory. This pipeline extracts
and then looks for a launch target, which for such an archive is the
installer itself — so the shortcut launches Setup rather than the game, and
the user has to complete the install once under Proton before the shortcut
means anything. Handling it properly needs a step this store does not have:
run the installer in the prefix, then re-scan for the real executable, the
way the wrapper stores let a vendor client install into a prefix. Local mode
at least *names* the case when the user labelled the archive ``(W_S)`` —
``InstallResult.metadata["is_installer"]`` — so the frontend can say so.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import foreign_installs_under, safe_rmtree
from unifideck.core.types import InstallResult, Result

from .archive import extract_archive, mkdir_p
from .exe_finder import find_executable
from .markers import (
    load_all_install_info,
    load_install_info,
    remove_install_info,
    save_install_info,
)
from .sources import ArchiveSource, ProgressCallback

logger = logging.getLogger(__name__)

STORE_NAME = "gamevault"


_MAX_DIR_ATTEMPTS = 20


def _free_game_dir(target_dir: Path, dir_name: str, game_id: str) -> Path:
    """A directory under *target_dir* that no other store's install owns.

    ``dir_name`` is the title reduced to a folder name, which is exactly what
    the other stores derive too — so ``<root>/Bastion`` is GOG's folder for
    Bastion *and* the natural one for a ``Bastion.zip`` in the vault. Sharing
    it is not survivable: GOG's install planner treats unrecognised data in
    its target as orphaned and deletes the directory, which is how a real
    device lost this extraction four times in one evening.

    Uniquifies rather than failing. The user asked for an install and there is
    a correct place to put it; refusing would just make them rename files.

    Blocking (reads games.map, stats directories) — call from a thread.
    """
    first = target_dir / dir_name
    for attempt in range(_MAX_DIR_ATTEMPTS):
        candidate = first if attempt == 0 else target_dir / (
            f"{dir_name} (GameVault)" if attempt == 1
            else f"{dir_name} (GameVault {attempt})"
        )
        foreign = foreign_installs_under(
            candidate, owner_key=f"{STORE_NAME}:{game_id}",
        )
        if not foreign:
            if candidate != first:
                logger.info(
                    "[GameVaultInstaller] %s is owned by another store; "
                    "installing to %s instead", first, candidate,
                )
            return candidate
        logger.warning(
            "[GameVaultInstaller] %s holds install(s) for %s; trying the "
            "next name", candidate, ", ".join(sorted(foreign)),
        )
    # Every candidate was taken, which means something is badly wrong with
    # games.map rather than with this install. Fall back to the plain name so
    # the install still happens; the ownership guard in ``uninstall_game``
    # still protects the user's archive.
    logger.error(
        "[GameVaultInstaller] no free directory near %s after %d tries; "
        "using it anyway", first, _MAX_DIR_ATTEMPTS,
    )
    return first


class GameVaultInstaller:
    """Acquire → extract → register, for whichever source is wired in."""

    def __init__(
        self,
        *,
        source: ArchiveSource,
        default_install_root: str,
    ) -> None:
        self._source = source
        self._default_install_root = Path(default_install_root).expanduser()

    # ── Public API ──────────────────────────────────────────────────

    async def install_game(
        self,
        game_id: str,
        *,
        install_path: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> InstallResult:
        """Install *game_id* from whatever this installer's source supplies."""
        target_dir = await self._prepare_target(install_path)
        acquired = None
        created_dir: Path | None = None
        try:
            acquired = await self._source.acquire(
                game_id, progress_callback=progress_callback,
            )
            if progress_callback:
                await progress_callback({"phase": "extracting"})

            game_dir = await asyncio.to_thread(
                _free_game_dir, target_dir, acquired.dir_name, game_id,
            )
            # Remember whether this run is the one that brings the directory
            # into existence. Only then may a cancellation delete it: an
            # extraction into a directory that was already there would take
            # whatever else was in it.
            if not await asyncio.to_thread(game_dir.exists):
                created_dir = game_dir
            await extract_archive(acquired.path, game_dir)

            exe_path = await asyncio.to_thread(
                find_executable,
                str(game_dir),
                prefer_native=acquired.prefer_native,
                title=acquired.title,
            )
            save_install_info(
                game_id,
                title=acquired.title,
                install_path=str(game_dir),
                exe_path=exe_path or "",
                archive_path=str(acquired.path),
            )
            return InstallResult(
                success=True,
                store=STORE_NAME,
                game_id=game_id,
                install_path=str(game_dir),
                metadata={
                    "exe_path": exe_path or "",
                    "title": acquired.title,
                    "is_installer": acquired.is_installer,
                },
            )
        except asyncio.CancelledError:
            # The user pressed Cancel. Nothing recorded this install yet —
            # the marker is only written on success — so without this the
            # half-extracted directory would be invisible to
            # ``uninstall_game`` ("Game not installed") and unreclaimable
            # from the UI, holding however many GB had landed.
            await self._discard_partial_extract(created_dir, game_id)
            raise
        except Exception as exc:
            logger.exception("[GameVaultInstaller] install_game failed")
            return InstallResult(
                success=False,
                error=str(exc),
                store=STORE_NAME,
                game_id=game_id,
            )
        finally:
            self._source.release(acquired)

    @staticmethod
    async def _discard_partial_extract(
        game_dir: Path | None, game_id: str,
    ) -> None:
        """Remove a directory this run created and did not finish filling.

        Re-checks ownership immediately before deleting rather than trusting
        the check ``_free_game_dir`` made: an extract can run for a long
        time, and another store may have claimed the folder in between.
        """
        if game_dir is None:
            return
        foreign = await asyncio.to_thread(
            foreign_installs_under, game_dir,
            owner_key=f"{STORE_NAME}:{game_id}",
        )
        if foreign:
            logger.warning(
                "[GameVaultInstaller] leaving cancelled extract at %s: now "
                "holds install(s) for %s", game_dir, ", ".join(sorted(foreign)),
            )
            return
        if await asyncio.to_thread(safe_rmtree, game_dir):
            logger.info(
                "[GameVaultInstaller] removed cancelled extract at %s",
                game_dir,
            )
        else:
            logger.warning(
                "[GameVaultInstaller] could not remove cancelled extract "
                "at %s", game_dir,
            )

    async def uninstall_game(self, game_id: str) -> Result:
        """Remove the extracted game and its marker.

        Only ever touches ``install_path``, the directory this pipeline
        created. The vault archive lives outside it, so it is not at risk:
        ``install_path`` is always ``<install root>/<game dir>``, and the
        extraction created that directory. An archive can only be inside it
        if the user put it there by hand, and then it is part of the folder
        they asked to remove.

        (An earlier version compared ``install_path`` against the marker's
        ``archive_path`` and, on a match, skipped the deletion and dropped the
        marker. That protected a file the pipeline cannot place there, and paid
        for it by orphaning the whole install — possibly tens of GB — with
        nothing left tracking it.)

        Deletion goes through :func:`safe_rmtree`, the shared guard the other
        stores' uninstallers use, because the install path is user-chosen and
        a mistyped one could otherwise point somewhere that matters.
        """
        info = self.get_install_info(game_id)
        if not info:
            return Result(
                success=False,
                error="Game not installed",
                store=STORE_NAME,
            )
        install_path = str(info.get("install_path", ""))
        if install_path:
            removed = await asyncio.to_thread(safe_rmtree, install_path)
            if removed:
                logger.info(
                    "[GameVaultInstaller] Removed install dir %s", install_path,
                )
            else:
                logger.warning(
                    "[GameVaultInstaller] Refused or failed to remove %s",
                    install_path,
                )
        remove_install_info(game_id)
        return Result(success=True, store=STORE_NAME)

    async def get_game_size(self, game_id: str) -> int | None:
        """Bytes the install will need, per the source."""
        return await self._source.size(game_id)

    # ── Marker helpers (called by store.py and the library reader) ──

    def get_install_info(self, game_id: str) -> dict[str, Any] | None:
        """The persisted install marker for *game_id*, verbatim.

        Deliberately does not guess. An earlier version re-scanned the
        install directory when ``exe_path`` was empty and wrote the guess
        back — which then flowed through ``get_library`` into the games.map
        row on every sync, overwriting whatever launch target the user had
        chosen in Change Executable. Choosing the executable matters more
        for this store than for any other, because a GameVault archive is
        whatever its owner uploaded; so the guess belongs at install time
        only, and fixing a bad one belongs to the user.
        """
        return load_install_info(game_id)

    def get_installed(self) -> dict[str, dict[str, Any]]:
        """``{game_id: marker}`` for every installed GameVault game."""
        return load_all_install_info()

    # ── Internals ───────────────────────────────────────────────────

    async def _prepare_target(self, install_path: str | None) -> Path:
        """Resolve and create the install root for this job.

        ``expanduser()`` reads $HOME and touches no filesystem, so it is not
        the blocking call ASYNC240 is looking for; the mkdir below is, and
        that one goes to a thread — it can hit a sleeping SD card or a
        network mount, and this coroutine shares the event loop with the
        download queue.
        """
        target_dir = (
            Path(install_path).expanduser()  # noqa: ASYNC240
            if install_path
            else self._default_install_root
        )
        await asyncio.to_thread(mkdir_p, target_dir)
        # The marker directory is created by ``save_install_info`` when there
        # is something to record, so it is not pre-made here.
        return target_dir
