"""Where a GameVault game comes from — the two ends of one pipeline.

A GameVault library is either a self-hosted server or a folder of archives on
this device. That is the *whole* difference between the two modes, and this
module is where it is allowed to live. Everything downstream — extraction,
executable discovery, the install marker, the never-truncate library rule,
uninstall — is one implementation shared by both.

Two seams, deliberately narrow:

``CatalogSource``
    answers "what games are there?" with :class:`Game` records that carry no
    install state. :class:`GameVaultLibraryReader` overlays that and enforces
    the raise-never-truncate invariant, once, for both modes.

``ArchiveSource``
    answers "give me the archive for this game, and take it back when I am
    done with it". Remote downloads it and deletes it; local hands over a
    path it already has and keeps it. :meth:`ArchiveSource.release` is why
    :meth:`GameVaultInstaller.install_game` needs no mode branch — deleting
    the user's own file is prevented by the source that owns it, not by an
    ``if`` in the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp

if TYPE_CHECKING:
    from unifideck.core.types import Game

    from .auth import GameVaultAuth

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_CHUNK_BYTES = 1024 * 1024
_REPORT_INTERVAL_S = 1.0


@dataclass(frozen=True)
class AcquiredArchive:
    """One archive, ready to extract, plus how to name what comes out of it.

    ``title`` and ``dir_name`` travel with the archive rather than being
    re-derived by the installer, because the two sources know different
    things: the server sends a ``Content-Disposition`` filename, while the
    vault scanner has already parsed the title out of the name and can give
    a directory that is not littered with ``(v1.2) (2021)`` tokens.
    """

    path: Path
    title: str
    dir_name: str
    # Both default False so a source that cannot know says nothing rather
    # than guessing. Remote is in that position today: the server serves
    # ``GameVersion.type``, but the library reader does not read it yet.
    prefer_native: bool = False
    is_installer: bool = False


class CatalogSource(Protocol):
    """Reads the list of games. Raises when it cannot — never truncates."""

    async def fetch(self) -> list[Game]:
        """Every game, with ``installed`` left False for the reader to fill."""
        ...


class ArchiveSource(Protocol):
    """Supplies the archive bytes for one game."""

    async def acquire(
        self, game_id: str, *, progress_callback: ProgressCallback | None,
    ) -> AcquiredArchive:
        """Put the archive on local disk and describe it."""
        ...

    def release(self, acquired: AcquiredArchive | None) -> None:
        """Called once the install finished or failed. Must never raise."""
        ...

    async def size(self, game_id: str) -> int | None:
        """Download/extract size in bytes, or None when unknown."""
        ...


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
            archive_path = staging / _archive_name_for(resp, game_id)
            downloaded = await _stream_to_file(
                resp.content, archive_path, _declared_length(resp),
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


def _archive_name_for(resp: Any, game_id: str) -> str:
    """The on-disk filename for a download, from the server's own header."""
    return _safe_archive_name(
        _parse_filename_from_cd(resp.headers.get("Content-Disposition", "")),
        game_id,
    )


def _declared_length(resp: Any) -> int:
    """Content-Length, or 0.

    It is absent for chunked transfers, and 0 yields ``pct=0`` rather than a
    division by zero.
    """
    return int(resp.headers.get("Content-Length") or resp.content_length or 0)


async def _stream_to_file(
    content: Any,
    archive_path: Path,
    total: int,
    progress_callback: ProgressCallback | None,
) -> int:
    """Write *content* to *archive_path*, reporting at most once a second.

    A download that does not finish takes its partial file with it. Nothing
    else can: on cancel or error the installer's ``finally`` calls
    ``release(None)``, because it never received an ``AcquiredArchive`` to
    release — so an abandoned multi-gigabyte body would sit in staging with
    no record that it exists. ``BaseException`` on purpose: cancellation is
    the common case here, and it is not an ``Exception``.
    """
    downloaded = 0
    last_report = 0.0
    start = time.monotonic()
    try:
        with archive_path.open("wb") as fh:
            async for chunk in content.iter_chunked(_CHUNK_BYTES):
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if progress_callback and now - last_report >= _REPORT_INTERVAL_S:
                    await progress_callback(
                        _progress_payload(downloaded, total, now - start),
                    )
                    last_report = now
    except BaseException:
        # Deliberately blocking: this runs while the task is being cancelled,
        # where any ``await`` re-raises immediately and would skip the
        # cleanup entirely. One unlink does not wait on I/O completion.
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)  # noqa: ASYNC240
        logger.info(
            "[GameVault/remote] discarded partial download %s",
            archive_path.name,
        )
        raise
    return downloaded


def _progress_payload(
    downloaded: int, total: int, elapsed: float,
) -> dict[str, Any]:
    """One ``DOWNLOAD_PROGRESS`` tick, in the worker's dict shape.

    ``phase`` only — no ``phase_message``: the UI localises from the phase,
    and the decorative message producers were deleted in register 45.
    """
    speed_bps = downloaded / elapsed if elapsed > 0 else 0.0
    remaining = total - downloaded
    return {
        "phase": "downloading",
        "percentage": round(downloaded / total * 100, 1) if total else 0,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "speed_bps": speed_bps,
        "eta_seconds": (
            int(remaining / speed_bps) if speed_bps > 0 and total > 0 else 0
        ),
    }


def _safe_archive_name(candidate: str | None, game_id: str) -> str:
    """A filename that cannot escape the download directory.

    ``Content-Disposition`` is written by the server, so the name in it is
    remote input: ``../../.ssh/authorized_keys`` is a valid header value.
    ``Path(...).name`` drops every directory component, and the dot-only
    names that survive it (``.``, ``..``) are rejected outright, so the
    result can only ever land directly inside the staging directory.
    """
    name = Path(candidate or "").name.strip()
    if not name or set(name) <= {"."}:
        return f"gamevault_{game_id}.bin"
    return name


def _parse_filename_from_cd(content_disposition: str) -> str | None:
    """Extract filename from Content-Disposition header."""
    m = re.search(r'filename\*?=["\']?([^"\';\r\n]+)["\']?', content_disposition)
    if m:
        name = m.group(1).strip()
        # Strip RFC 5987 charset prefix, e.g. "UTF-8''Filename.zip"
        if "''" in name:
            name = name.split("''", 1)[1]
        return name.strip('"\'')
    return None


def safe_dir_name(title: str) -> str:
    """A title reduced to something safe to use as a directory name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")
    return cleaned or "game"


__all__ = [
    "AcquiredArchive",
    "ArchiveSource",
    "CatalogSource",
    "LocalArchiveSource",
    "LocalVaultLocator",
    "ProgressCallback",
    "RemoteArchiveSource",
    "safe_dir_name",
]
