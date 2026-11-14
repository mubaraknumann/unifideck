"""Streaming and filename handling for downloaded game archives."""
from __future__ import annotations

import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from .archive_source import ProgressCallback

logger = logging.getLogger(__name__)
_CHUNK_BYTES = 1024 * 1024
_REPORT_INTERVAL_S = 1.0


def archive_name_for(resp: Any, game_id: str, *, store: str = "gamevault") -> str:
    """The on-disk filename for a download, from the server's own header."""
    return safe_archive_name(
        parse_filename_from_cd(resp.headers.get("Content-Disposition", "")),
        game_id, store=store,
    )


def declared_length(resp: Any) -> int:
    """Content-Length, or 0.

    It is absent for chunked transfers, and 0 makes the payload report a
    percentage of zero rather than dividing by it.
    """
    return int(resp.headers.get("Content-Length") or resp.content_length or 0)


async def stream_to_file(
    content: Any,
    archive_path: Path,
    total: int,
    progress_callback: ProgressCallback | None,
) -> int:
    """Write *content* to *archive_path*, reporting at most once a second.

    A download that does not finish deletes its partial file. The installer
    cannot do it for us: when acquisition itself fails, its ``finally`` calls
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
                        progress_payload(downloaded, total, now - start),
                    )
                    last_report = now
    except BaseException:
        # Synchronous on purpose: the cleanup must not depend on the cancelled
        # task being scheduled again, and one unlink is short enough to run on
        # the event loop.
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)  # noqa: ASYNC240
        logger.info(
            "[ArchiveDownload] discarded partial download %s",
            archive_path.name,
        )
        raise
    return downloaded


def progress_payload(
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


def safe_archive_name(candidate: str | None, game_id: str, *, store: str = "gamevault") -> str:
    """A filename that cannot escape the download directory.

    ``Content-Disposition`` is written by the server, so the name in it is
    remote input: ``../../.ssh/authorized_keys`` is a valid header value.
    ``Path(...).name`` drops every directory component, and the dot-only
    names that survive it (``.``, ``..``) are rejected outright, so the
    result can only ever land directly inside the staging directory.
    """
    name = Path(candidate or "").name.strip()
    if not name or set(name) <= {"."}:
        return f"{store}_{game_id}.bin"
    return name


def parse_filename_from_cd(content_disposition: str) -> str | None:
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
