"""Stream a complete ROM from RomM into a staging directory this source owns."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from unifideck.stores.shared.archive_download import (
    archive_name_for,
    declared_length,
    progress_payload,
    safe_dir_name,
    stream_to_file,
)
from unifideck.stores.shared.archive_source import AcquiredArchive, ProgressCallback

from .auth import RomMAuth
from .client import RomMClient
from .library import RomMCatalog

logger = logging.getLogger(__name__)


class RomMArchiveSource:
    """Downloads one ROM per call into a private staging directory.

    Each acquisition owns a fresh directory, so two games whose server files
    share a name cannot overwrite each other, and releasing it can never touch
    anything the user placed in the staging root.
    """

    def __init__(self, auth: RomMAuth, catalog: RomMCatalog, *, download_dir: str) -> None:
        self._auth = auth
        self._catalog = catalog
        self._download_dir = Path(download_dir).expanduser()
        self._owned: set[Path] = set()

    def _staging_root(self) -> Path:
        override = self._auth.download_dir
        return Path(override).expanduser() if override else self._download_dir

    async def acquire(
        self, game_id: str, *, progress_callback: ProgressCallback | None,
    ) -> AcquiredArchive:
        client = self._auth.client()
        rom = await self._catalog.resolve(game_id, client=client)
        root = self._staging_root()
        job = await asyncio.to_thread(self._make_job_dir, root)
        try:
            path, downloaded = await self._download(client, rom, job, progress_callback)
        except BaseException:
            self._discard(job)
            raise
        logger.info("[RomM] Downloaded %s (%d bytes)", path.name, downloaded)
        title = (
            _text(rom.get("name")) or _text(rom.get("fs_name_no_ext")) or path.stem
        )
        return AcquiredArchive(
            path=path, title=title, dir_name=safe_dir_name(title),
            prefer_native=rom.get("platform_slug") == "linux",
        )

    async def _download(
        self,
        client: RomMClient,
        rom: dict[str, Any],
        job: Path,
        progress_callback: ProgressCallback | None,
    ) -> tuple[Path, int]:
        async with client.open_content(rom["id"], rom["fs_name"]) as resp:
            path = job / self._file_name(resp, rom)
            downloaded = await stream_to_file(
                resp.content, path, declared_length(resp), progress_callback,
            )
        if progress_callback:
            # Close the bar at 100%: the total the callback was given is the
            # response's Content-Length, and a generated ZIP does not send one,
            # so the last tick reports what was actually transferred instead.
            await progress_callback(progress_payload(downloaded, downloaded, 0.0))
        return path, downloaded

    def _make_job_dir(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        job = Path(tempfile.mkdtemp(dir=root, prefix="romm-"))
        self._owned.add(job)
        return job

    @staticmethod
    def _file_name(resp: Any, rom: dict[str, Any]) -> str:
        """Name the file for what arrived, not for the server's file.

        A multi-file ROM's ``fs_name`` is a folder; RomM serves it as a ZIP,
        so a name taken from the folder would hide that. Extraction detects
        the format from magic bytes either way.
        """
        rom_id = str(rom["id"])
        if resp.headers.get("Content-Disposition"):
            return archive_name_for(resp, rom_id, store="romm")
        if rom.get("has_multiple_files") or rom.get("has_simple_single_file") is False:
            return f"romm_{rom_id}.zip"
        return _plain_name(rom)

    def release(self, acquired: AcquiredArchive | None) -> None:
        """Delete the staging directory this download created.

        Runs from the installer's ``finally``, so it must never raise, and it
        only removes a directory this source made: an archive the user brought
        is never in ``_owned``.
        """
        if acquired is None:
            return
        job = acquired.path.parent
        if job in self._owned:
            self._discard(job)

    def _discard(self, job: Path) -> None:
        self._owned.discard(job)
        with contextlib.suppress(OSError):
            shutil.rmtree(job)

    async def size(self, game_id: str) -> int | None:
        return await self._catalog.size(game_id)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _plain_name(rom: dict[str, Any]) -> str:
    name = Path(_text(rom.get("fs_name"))).name
    return name if name and set(name) > {"."} else f"romm_{rom['id']}.bin"
