"""Acquire, extract, register, and remove games supplied as archives.

One pipeline, for whichever source a store wires in:

    1. source.acquire()  →  archive on local disk (a download, or already there)
    2. extract           →  <install root>/<dir_name>/        (archive.py)
    3. find the exe      →  best launch target                (launch_target.py)
    4. write the record  →  the install record                (install_records.py)
    5. source.release()  →  delete the staged copy, or keep the user's file

Step 5 runs from a ``finally`` on every path, and the source decides what
"release" means: a remote source deletes the staging directory it created for
that download, a local one does nothing because the file is the user's only
copy. Deleting the wrong one is prevented by ownership, not by an ``if``.

**Known gap: an archive that is an installer.** An archive library holds
whatever its owner uploaded, and that is often a repack or an offline
installer rather than a ready-to-run game directory. The launch-target scorer
rejects names like ``setup`` and ``install``, but falls back to the best
rejected candidate when nothing else qualifies, so the shortcut for such an
archive launches Setup rather than the game, and the user has to complete the
install once under Proton before the shortcut means anything. Handling it
properly needs a step this pipeline does not have: run the installer in the
prefix, then re-scan for the real executable, the way the wrapper stores let a
vendor client install into a prefix. GameVault's local vault sets
``AcquiredArchive.is_installer`` from the archive's type token, and the flag
travels in the install result's metadata; nothing reads it yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import foreign_installs_under, safe_rmtree
from unifideck.core.types import InstallResult, Result
from unifideck.stores.shared.launch_target import find_executable

from .archive import extract_archive, mkdir_p
from .archive_source import ArchiveSource, ProgressCallback
from .install_records import InstallRecords

logger = logging.getLogger(__name__)

_MAX_DIR_ATTEMPTS = 20


def _holds_foreign_files(candidate: Path, own_path: str | None) -> bool:
    """True if *candidate* is occupied and is not the recorded install."""
    if own_path is not None and str(candidate) == own_path:
        return False
    if candidate.is_symlink():
        return True
    if not candidate.exists():
        return False
    if not candidate.is_dir():
        return True
    return any(candidate.iterdir())


def _free_game_dir(
    target_dir: Path,
    dir_name: str,
    game_id: str,
    store: str,
    own_path: str | None = None,
) -> Path:
    """A directory under *target_dir* that no other store's install owns.

    ``dir_name`` is the title reduced to a folder name, which is exactly what
    the other stores derive too — so ``<root>/Bastion`` is GOG's folder for
    Bastion *and* the natural one for a ``Bastion.zip`` in the vault. Sharing
    it loses data: GOG's install planner treats unrecognised data in its
    target as orphaned and deletes the directory, which is how a real device
    lost this extraction four times in one evening.

    A directory that already holds files is skipped unless it is this game's
    own previous install (*own_path*): extracting into it would mix the
    archive with files we did not put there, and uninstall would later delete
    them all.

    Uses a distinct name rather than taking the plain one, because the user
    asked for an install and there is a correct place to put it. Gives up
    after ``_MAX_DIR_ATTEMPTS`` candidate names; that many occupied folders
    means leftovers or damaged state, not a popular title.

    Blocking (reads games.map, stats directories) — call from a thread.
    """
    label = {"gamevault": "GameVault", "romm": "RomM"}.get(store, store)
    first = target_dir / dir_name
    for attempt in range(_MAX_DIR_ATTEMPTS):
        candidate = first if attempt == 0 else target_dir / (
            f"{dir_name} ({label})" if attempt == 1
            else f"{dir_name} ({label} {attempt})"
        )
        foreign = foreign_installs_under(
            candidate, owner_key=f"{store}:{game_id}",
        )
        if not foreign and not _holds_foreign_files(candidate, own_path):
            if candidate != first:
                logger.info(
                    "[ArchiveInstaller] %s is owned by another store; "
                    "installing to %s instead", first, candidate,
                )
            return candidate
        logger.warning(
            "[ArchiveInstaller] %s is not free (%s); trying the next name",
            candidate, ", ".join(sorted(foreign)) or "holds other files",
        )
    raise RuntimeError("No unowned installation directory is available")


class ArchiveInstaller:
    """Acquire → extract → register, for whichever source is wired in."""

    def __init__(
        self,
        *,
        store: str,
        source: ArchiveSource,
        default_install_root: str,
        normalize: Callable[[Path], None] | None = None,
    ) -> None:
        self._store = store
        self._records = InstallRecords(store)
        self._normalize = normalize
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

            previous = self._records.load(game_id) or {}
            own_path = previous.get("install_path")
            game_dir = await asyncio.to_thread(
                _free_game_dir, target_dir, acquired.dir_name, game_id, self._store,
                own_path if isinstance(own_path, str) else None,
            )
            # Remember whether this run is the one that brings the directory
            # into existence. Only then may a cancellation delete it: an
            # extraction into a directory that was already there would take
            # whatever else was in it.
            if not await asyncio.to_thread(game_dir.exists):
                created_dir = game_dir
            await extract_archive(acquired.path, game_dir)
            if self._normalize is not None:
                await asyncio.to_thread(self._normalize, game_dir)

            exe_path = await asyncio.to_thread(
                find_executable,
                str(game_dir),
                prefer_native=acquired.prefer_native,
                title=acquired.title,
            )
            self._records.save(
                game_id,
                title=acquired.title,
                install_path=str(game_dir),
                exe_path=exe_path or "",
                archive_path=str(acquired.path),
            )
            return InstallResult(
                success=True,
                store=self._store,
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
            # the record is only written on success — so without this the
            # half-extracted directory would be invisible to
            # ``uninstall_game`` ("Game not installed") and unreclaimable
            # from the UI, holding however many GB had landed.
            await self._discard_partial_extract(created_dir, game_id)
            raise
        except Exception as exc:
            logger.exception("[ArchiveInstaller] install_game failed")
            return InstallResult(
                success=False,
                error=str(exc),
                store=self._store,
                game_id=game_id,
            )
        finally:
            self._source.release(acquired)

    async def _discard_partial_extract(
        self, game_dir: Path | None, game_id: str,
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
            owner_key=f"{self._store}:{game_id}",
        )
        if foreign:
            logger.warning(
                "[ArchiveInstaller] leaving cancelled extract at %s: now "
                "holds install(s) for %s", game_dir, ", ".join(sorted(foreign)),
            )
            return
        if await asyncio.to_thread(safe_rmtree, game_dir):
            logger.info(
                "[ArchiveInstaller] removed cancelled extract at %s",
                game_dir,
            )
        else:
            logger.warning(
                "[ArchiveInstaller] could not remove cancelled extract "
                "at %s", game_dir,
            )

    async def uninstall_game(self, game_id: str) -> Result:
        """Remove the extracted game and its record.

        Only ever touches ``install_path``, the directory this pipeline
        created. The vault archive lives outside it, so it is not at risk:
        ``install_path`` is always ``<install root>/<game dir>``, and the
        extraction created that directory. An archive can only be inside it
        if the user put it there by hand, and then it is part of the folder
        they asked to remove.

        (An earlier version compared ``install_path`` against the record's
        ``archive_path`` and, on a match, skipped the deletion and dropped the
        record. That protected a file the pipeline cannot place there, and paid
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
                store=self._store,
            )
        install_path = str(info.get("install_path", ""))
        if install_path:
            removed = await asyncio.to_thread(safe_rmtree, install_path)
            if removed:
                logger.info(
                    "[ArchiveInstaller] Removed install dir %s", install_path,
                )
            else:
                logger.warning(
                    "[ArchiveInstaller] Refused or failed to remove %s",
                    install_path,
                )
        self._records.remove(game_id)
        return Result(success=True, store=self._store)

    async def get_game_size(self, game_id: str) -> int | None:
        """Size in bytes as the source reports it, or ``None``."""
        return await self._source.size(game_id)

    # ── Install records (store surface and the library reader) ──

    def get_install_info(self, game_id: str) -> dict[str, Any] | None:
        """The persisted install record for *game_id*, verbatim.

        Deliberately does not guess. An earlier version re-scanned the
        install directory when ``exe_path`` was empty and wrote the guess
        back — which then flowed through ``get_library`` into the games.map
        row on every sync, overwriting whatever launch target the user had
        chosen in Change Executable. Choosing the executable matters more
        for this store than for any other, because an archive contains
        whatever its owner uploaded; so the guess belongs at install time
        only, and fixing a bad one belongs to the user.
        """
        return self._records.load(game_id)

    def get_installed(self) -> dict[str, dict[str, Any]]:
        """``{game_id: record}`` for every installed game in this store."""
        return self._records.load_all()

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
        # The record directory is created by ``InstallRecords.save`` when there
        # is something to record, so it is not pre-made here.
        return target_dir
