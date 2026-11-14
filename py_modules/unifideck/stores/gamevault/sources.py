"""Remote and local GameVault archive sources."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import aiohttp

from unifideck.stores.shared.archive_download import (
    archive_name_for,
    declared_length,
    stream_to_file,
)
from unifideck.stores.shared.archive_source import AcquiredArchive, ProgressCallback

if TYPE_CHECKING:
    from .auth import GameVaultAuth

logger = logging.getLogger(__name__)

# ── Remote: the archive lives on the user's server ──────────────────────────

class RemoteArchiveSource:
    """Streams the archive down from a GameVault server, then deletes it.

    Owns ``download_dir``, which is a remote-only concept: the archive and
    the unpacked game have to fit somewhere at the same time, and on a Deck
    that is often two different drives. Local mode has no such staging step,
    which is why the setting does not appear in its form.
    """

    def __init__(self, auth: GameVaultAuth, *, download_dir: str) -> None:
        self._auth = auth
        self._download_dir = Path(download_dir).expanduser()

    async def _session_context(self) -> tuple[str, dict[str, str], bool]:
        headers = await self._auth.get_auth_headers()
        if not headers:
            raise RuntimeError("Not authenticated")
        return self._auth.server_url or "", headers, self._auth.verify_ssl

    def _staging_dir(self) -> Path:
        """Per-install override → saved setting → configured default."""
        override = self._auth.download_dir
        return Path(override).expanduser() if override else self._download_dir

    async def acquire(
        self, game_id: str, *, progress_callback: ProgressCallback | None,
    ) -> AcquiredArchive:
        server_url, headers, verify_ssl = await self._session_context()
        staging = self._staging_dir()
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
        return await _download_archive(
            url=f"{server_url}/api/games/{game_id}/download",
            headers=headers,
            verify_ssl=verify_ssl,
            staging=staging,
            game_id=game_id,
            progress_callback=progress_callback,
        )

    def release(self, acquired: AcquiredArchive | None) -> None:
        """Delete the staged archive.

        Runs from the installer's ``finally``, so it must never raise: a
        failure here would replace the real install error with an unlink
        error. Best-effort by design — the archive is a cache, and the worst
        case of leaving one behind is wasted disk, whereas propagating would
        lose the diagnosis of why the install failed.
        """
        if acquired is None or not acquired.path.exists():
            return
        try:
            acquired.path.unlink()
            logger.info("[GameVault/remote] Removed archive %s", acquired.path)
        except Exception as exc:
            logger.warning(
                "[GameVault/remote] Could not delete archive %s: %s",
                acquired.path, exc,
            )

    async def size(self, game_id: str) -> int | None:
        """HEAD /api/games/{id}/download → Content-Length."""
        try:
            server_url, headers, verify_ssl = await self._session_context()
        except RuntimeError:
            return None
        url = f"{server_url}/api/games/{game_id}/download"
        try:
            connector = aiohttp.TCPConnector(ssl=verify_ssl)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.head(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as resp:
                    cl = resp.headers.get("Content-Length", "")
                    if cl.isdigit():
                        return int(cl)
        except Exception as exc:
            logger.debug("[GameVault/remote] size(%s) error: %s", game_id, exc)
        return None


# ── Local: the archive is already on this device ────────────────────────────

class LocalArchiveSource:
    """Hands over an archive from the user's vault folder and keeps it.

    :meth:`release` is a no-op, and that is the entire safety story for the
    "uninstall must not eat my zip" requirement: the pipeline calls release
    on every path, success or failure, and this source simply declines. The
    extracted copy still gets deleted by uninstall, because that lives under
    the install root, which :class:`LocalVaultLocator` guarantees is outside
    the vault.
    """

    def __init__(self, locator: LocalVaultLocator) -> None:
        self._locator = locator

    async def acquire(
        self, game_id: str, *, progress_callback: ProgressCallback | None,
    ) -> AcquiredArchive:
        acquired = await asyncio.to_thread(self._locator.resolve, game_id)
        if acquired is None:
            raise RuntimeError(
                f"No archive for {game_id} in the vault folder — it may have "
                f"been renamed or removed since the last library sync",
            )
        if progress_callback:
            # Local mode skips straight to extraction. The frontend renders
            # the phase, and a job that never reports "downloading" would
            # otherwise sit blank until the first extract tick.
            await progress_callback({"phase": "extracting", "percentage": 0})
        return acquired

    def release(self, acquired: AcquiredArchive | None) -> None:
        """Keep the file. It is the user's, and it is the only copy."""

    async def size(self, game_id: str) -> int | None:
        acquired = await asyncio.to_thread(self._locator.resolve, game_id)
        if acquired is None:
            return None
        try:
            return await asyncio.to_thread(_file_size, acquired.path)
        except OSError:
            return None


class LocalVaultLocator(Protocol):
    """Maps a game id back to the archive it came from.

    Kept as a protocol so :class:`LocalArchiveSource` does not import the
    scanner and the scanner does not import the source — the id scheme is
    the scanner's business, and this is the one thing the installer needs
    to know about it.
    """

    def resolve(self, game_id: str) -> AcquiredArchive | None:
        """The archive for *game_id*, or None if it is no longer there."""
        ...


def _file_size(path: Path) -> int:
    return path.stat().st_size


# ── Shared HTTP plumbing ────────────────────────────────────────────────────

async def _download_archive(
    *,
    url: str,
    headers: dict[str, str],
    verify_ssl: bool,
    staging: Path,
    game_id: str,
    progress_callback: ProgressCallback | None,
) -> AcquiredArchive:
    """Stream one archive to *staging* and describe what landed.

    A module function rather than a method so ``acquire`` stays under the
    fan-out cap: opening the session, naming the file, sizing it and
    streaming it are one step of that pipeline, not four.
    """
    connector = aiohttp.TCPConnector(ssl=verify_ssl)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=0),  # no overall timeout
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download returned HTTP {resp.status}")
            archive_path = staging / archive_name_for(resp, game_id)
            downloaded = await stream_to_file(
                resp.content, archive_path, declared_length(resp),
                progress_callback,
            )

    logger.info(
        "[GameVault/remote] Downloaded %s (%d bytes) to %s",
        archive_path.name, downloaded, staging,
    )
    return AcquiredArchive(
        path=archive_path,
        title=archive_path.stem,
        dir_name=archive_path.stem,
    )
