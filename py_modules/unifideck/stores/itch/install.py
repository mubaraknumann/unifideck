"""Install, update and uninstall itch.io games through butlerd.

One install = ``Install.Queue`` + ``Install.Perform`` on a dedicated
connection, so that connection's ``Progress`` notifications belong to this
install alone. Measured behaviour that shapes the code (butler 15.31.0):

* ``Install.Queue`` needs an *install location* id; a plain folder is refused
  ("When caveId is unspecified, installLocationId must be set"). Each storage
  root the user picks is registered once with ``Install.Locations.Add``.
* butler **ignores** the ``installFolder`` we pass and installs to
  ``<location>/<game-url-slug>`` (``fear-assessment``). The real folder is
  whatever Queue answers, and the cross-store collision check runs on that.
* **Neither call is quick.** Queue probes the remote archive over HTTP before
  answering (one took 1 min 04 s), and Perform extracts *while* downloading:
  a slow CDN path trickled at ~20 KB/s for 15 minutes on a live socket. So
  there is no RPC deadline on either; a watchdog fails the install only when
  the connection has gone quiet (no ``Progress`` advance and no ``Log`` line)
  for ``stall_s``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.core.manifest import write_manifest
from unifideck.core.safe_delete import foreign_installs_under, safe_rmtree
from unifideck.core.types import InstallResult, Result

from .butlerd import ButlerDaemon, ButlerdConnection, ButlerdError
from .exe import resolve_launch_target, upload_platform
from .library import STORE, ItchLibraryReader
from .progress import ProgressCallback, ProgressTracker
from .uploads import choose_upload

logger = logging.getLogger(__name__)


def _fail(game_id: str, error: str) -> InstallResult:
    logger.warning("[itch] install %s failed: %s", game_id, error)
    return InstallResult(success=False, game_id=game_id, store=STORE, error=error)


#: butler stages every install in ``<location>/downloads/<install-id>`` and
#: ignores the ``stagingFolder`` we pass (measured). These files mark a
#: directory there as butler's staging, so the sweep touches nothing else.
_STAGING_MARKERS = ("operate-context.json", "install-state.dat")


def sweep_staging(root: str) -> list[str]:
    """Remove butler staging folders left under *root* by abandoned installs.

    Safe to run before an install because the download queue runs one install
    at a time. A queued-but-not-performed install is not reachable by
    ``Install.Cancel`` (it answered ``didCancel: false``), so its staging is
    only ever cleaned here.
    """
    downloads = Path(root) / "downloads"
    removed: list[str] = []
    if not downloads.is_dir():
        return removed
    for child in downloads.iterdir():
        if child.is_dir() and any((child / m).exists() for m in _STAGING_MARKERS):
            if safe_rmtree(child):
                removed.append(child.name)
    if removed:
        logger.info("[itch] removed abandoned butler staging: %s", ", ".join(removed))
    with contextlib.suppress(OSError):
        downloads.rmdir()  # only succeeds when empty
    return removed


def _prepare_root(root: str) -> str:
    """Normalised absolute *root*, created if missing."""
    path = os.path.normpath(os.path.expanduser(root))
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def _location_for(locations: list[dict[str, Any]], root: str) -> str | None:
    """The id of the butler install location at *root*, if one is registered."""
    for loc in locations:
        if os.path.normpath(str(loc.get("path"))) == root:
            return str(loc["id"])
    return None


def _remove_if_ours(folder: str, owner_key: str) -> None:
    """Delete *folder* unless games.map says another store installed there."""
    if os.path.exists(folder) and not foreign_installs_under(folder, owner_key=owner_key):
        safe_rmtree(folder)


@dataclass(frozen=True)
class _Job:
    """One queued install or update."""

    game_id: str
    game: dict[str, Any]
    upload: dict[str, Any]
    queue: dict[str, Any]
    root: str


class ItchInstaller:
    """The install/update/uninstall pipeline for one daemon."""

    def __init__(
        self, daemon: ButlerDaemon, library: ItchLibraryReader,
        *, default_install_root: str, stall_s: float,
    ) -> None:
        self._daemon = daemon
        self._library = library
        self._default_root = default_install_root
        self._stall_s = stall_s

    async def install(
        self, game_id: str, base_path: str | None,
        progress_cb: ProgressCallback | None,
    ) -> InstallResult:
        """Install the best upload of *game_id* under *base_path*."""
        try:
            profile = await self._library.profile_id()
            if profile is None:
                return _fail(game_id, "itch_not_signed_in: itch.io login required")
            game = await self._library.owned_game(game_id)
            if game is None:
                return _fail(game_id, "itch_game_not_found")
            await self._drop_stale_cave(game_id)
            uploads = await self._daemon.call(
                "Fetch.GameUploads",
                {"gameId": int(game_id), "compatible": False, "fresh": True},
            )
            upload = choose_upload(uploads.get("uploads") or [])
            if upload is None:
                return _fail(game_id, "itch_no_installable_upload: no Linux or "
                                      "Windows build of this game can be installed")
            root = await asyncio.to_thread(_prepare_root, base_path or self._default_root)
            location = await self._ensure_location(root)
        except ButlerdError as e:
            return _fail(game_id, f"itch_install_failed: {e}")
        queue = {"game": game, "upload": upload, "installLocationId": location,
                 "profileId": profile, "reason": "install"}
        return await self._run(_Job(game_id, game, upload, queue, root), progress_cb)

    async def update(
        self, game_id: str, progress_cb: ProgressCallback | None,
    ) -> InstallResult:
        """Apply butler's first update choice to the installed cave."""
        try:
            cave = (await self._library.installed_map()).get(game_id)
            if cave is None:
                return _fail(game_id, "itch_not_installed")
            found = await self._daemon.call("CheckUpdate", {"caveIds": [cave["cave_id"]]})
            updates = found.get("updates") or []
            if not updates or not updates[0].get("choices"):
                return InstallResult(success=True, game_id=game_id, store=STORE,
                                     install_path=cave["install_path"])
            choice = updates[0]["choices"][0]
            game = updates[0].get("game") or await self._library.owned_game(game_id) or {}
        except ButlerdError as e:
            return _fail(game_id, f"itch_update_failed: {e}")
        queue = {"caveId": cave["cave_id"], "game": game, "upload": choice.get("upload"),
                 "build": choice.get("build"), "reason": "update"}
        job = _Job(game_id, game, choice.get("upload") or {}, queue,
                   str(Path(cave["install_path"]).parent))
        return await self._run(job, progress_cb)

    async def uninstall(self, game_id: str) -> Result:
        """``Uninstall.Perform`` the cave, then make sure its folder is gone."""
        try:
            cave = (await self._library.installed_map()).get(game_id)
            if cave is None:
                return Result(success=True, store=STORE)
            await self._daemon.call(
                "Uninstall.Perform", {"caveId": cave["cave_id"]}, timeout=600,
            )
        except ButlerdError as e:
            return Result(success=False, store=STORE, error=f"itch_uninstall_failed: {e}")
        folder = cave["install_path"]
        await asyncio.to_thread(_remove_if_ours, folder, f"{STORE}:{game_id}")
        return Result(success=True, store=STORE)

    async def check_for_updates(self) -> list[str]:
        """Game ids whose installed cave has an update available."""
        caves = await self._library.installed_map()
        if not caves:
            return []
        by_cave = {c["cave_id"]: gid for gid, c in caves.items()}
        found = await self._daemon.call(
            "CheckUpdate", {"caveIds": list(by_cave)}, timeout=300,
        )
        return [by_cave[u["caveId"]] for u in found.get("updates") or []
                if u.get("caveId") in by_cave]

    async def _run(self, job: _Job, progress_cb: ProgressCallback | None) -> InstallResult:
        await asyncio.to_thread(sweep_staging, job.root)
        tracker = ProgressTracker(progress_cb, stall_s=self._stall_s)
        conn = await self._daemon.connect(on_notification=tracker.on_notification)
        try:
            return await self._queue_and_perform(conn, tracker, job)
        finally:
            await conn.close()

    async def _queue_and_perform(
        self, conn: ButlerdConnection, tracker: ProgressTracker, job: _Job,
    ) -> InstallResult:
        await tracker.phase("preparing")
        try:
            queued = await tracker.watch(conn.call("Install.Queue", job.queue, timeout=None))
        except asyncio.CancelledError:
            # butler keeps probing after we stop listening and leaves its
            # staging behind; Install.Cancel does not reach a queued install.
            await asyncio.to_thread(sweep_staging, job.root)
            raise
        if isinstance(queued, str):
            await asyncio.to_thread(sweep_staging, job.root)
            return _fail(job.game_id, queued)
        folder = str(queued["installFolder"])
        existed = await asyncio.to_thread(os.path.exists, folder)
        if job.queue["reason"] == "install" and foreign_installs_under(
            folder, owner_key=f"{STORE}:{job.game_id}",
        ):
            await self._abandon(queued, folder, existed=True)
            return _fail(job.game_id, f"itch_folder_in_use: {folder} already holds "
                                      "a game from another store")
        await tracker.phase("downloading")
        perform = conn.call("Install.Perform", {
            "id": queued["id"], "stagingFolder": queued["stagingFolder"],
        }, timeout=None)
        try:
            outcome = await tracker.watch(perform)
        except asyncio.CancelledError:
            await self._abandon(queued, folder, existed)
            raise
        if isinstance(outcome, str):
            await self._abandon(queued, folder, existed)
            return _fail(job.game_id, outcome)
        # butler removes its staging folder on success but leaves the empty
        # ``<root>/downloads`` behind in the user's Games folder.
        await asyncio.to_thread(sweep_staging, job.root)
        return await self._finish(job, folder)

    async def _finish(self, job: _Job, folder: str) -> InstallResult:
        game_id, upload = job.game_id, job.upload
        title = str(job.game.get("title") or game_id)
        platform = upload_platform(upload)
        exe = await asyncio.to_thread(resolve_launch_target, folder, platform, title)
        if not exe:
            logger.error(
                "[itch] %s installed to %s but no runnable file was found there; "
                "the shortcut will not launch until one is chosen with "
                "Change Executable", title, folder,
            )
        rel = str(Path(exe).relative_to(folder)) if exe else ""
        await write_manifest(folder, STORE, game_id, title, rel,
                             platform="linux" if exe and not exe.endswith(".exe") else "windows")
        logger.info("[itch] installed %s (%s upload) → %s, launch %s",
                    title, platform, folder, rel or "<none>")
        return InstallResult(
            success=True, game_id=game_id, store=STORE, install_path=folder,
            size_bytes=int(upload.get("size") or 0),
            metadata={"exe_path": exe or "", "platform": platform},
        )

    async def _abandon(
        self, queued: dict[str, Any], folder: str, existed: bool,
    ) -> None:
        """Cancel butler's side, then remove a folder this install created."""
        with contextlib.suppress(ButlerdError, TimeoutError, OSError):
            await asyncio.shield(self._daemon.call(
                "Install.Cancel", {"id": queued["id"]}, timeout=15,
            ))
        for path in (queued.get("stagingFolder"), None if existed else folder):
            if path:
                await asyncio.to_thread(safe_rmtree, path)
        staging = queued.get("stagingFolder")
        if staging:
            # ``<location>/downloads`` exists only for butler's staging.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.rmdir, os.path.dirname(staging))

    async def _ensure_location(self, root: str) -> str:
        """The butler install-location id for *root* (prepared), registering it once."""
        found = await self._daemon.call("Install.Locations.List")
        existing = _location_for(found.get("installLocations") or [], root)
        if existing:
            return existing
        added = await self._daemon.call("Install.Locations.Add", {"path": root})
        return str(added["installLocation"]["id"])

    async def _drop_stale_cave(self, game_id: str) -> None:
        """Forget a cave whose folder no longer exists.

        butler's record outlives the files (a manual delete, a moved SD card),
        and a stale record would turn the next install into an "already
        installed" no-op. That is the class ``core/stale_installs`` handles for nile.
        """
        cave = (await self._library.installed_map()).get(game_id)
        if cave and not await asyncio.to_thread(os.path.exists, cave["install_path"]):
            logger.info("[itch] dropping stale cave %s for %s (folder gone)",
                        cave["cave_id"], game_id)
            with contextlib.suppress(ButlerdError):
                await self._daemon.call("Uninstall.Perform", {"caveId": cave["cave_id"]})
