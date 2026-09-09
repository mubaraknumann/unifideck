"""GameVault archive handling — detect the format, unpack it.

The archive is whatever its owner put in the library, so the format is
discovered from magic bytes rather than the filename, and unpacking anything
but a zip shells out to whichever tool the host has.

**One ladder for every non-zip format, ``bsdtar`` first.** This used to be two
ladders: rar tried ``bsdtar`` → ``unrar`` → ``7z``, while 7z required the
``7z`` binary and nothing else. Stock SteamOS does not ship ``7z`` — it ships
``bsdtar``, whose libarchive reads 7z, rar, iso and cab — so a ``.7z`` upload
failed on an untouched Deck *after* the user had waited out a multi-gigabyte
download. The two ladders had no reason to differ; the difference was just
where each was written.

**Detection is what limits the format list, not the ladder.** ``detect_format``
once recognised zip, rar and 7z only, while the local-vault scanner indexed
tar, gzip, iso, wim and cab as well (``filename.ARCHIVE_EXTENSIONS``). Those
files got a Steam shortcut and then failed at install with "Unknown archive
format" — a promise the library made and the installer could not keep, even
though ``bsdtar`` reads every one of them. They now map to
``_ARCH_LIBARCHIVE``, a passthrough that carries no codec claim of its own: it
means "the ladder knows what this is". A format the host's libarchive was
built without still fails, but with the ladder's "tried bsdtar, 7z" message,
which names the missing tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import threading
import zipfile
from pathlib import Path

from unifideck.stores.shared.cli_install_helpers import terminate_process_tree

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[GameVault archive]"

_ARCH_ZIP = "zip"
_ARCH_RAR = "rar"
_ARCH_7Z = "7z"
# Anything else the extractor ladder can unpack. Kept as one value because
# nothing downstream branches on which of them it is.
_ARCH_LIBARCHIVE = "libarchive"

# Magic bytes read from the head of the file, longest match first so a
# compressed tar is identified by its compression wrapper.
_HEAD_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xfd7zXZ\x00", _ARCH_LIBARCHIVE),   # xz
    (b"\x28\xb5\x2f\xfd", _ARCH_LIBARCHIVE),  # zstd
    (b"MSWIM\x00\x00\x00", _ARCH_LIBARCHIVE),  # wim
    (b"MSCF", _ARCH_LIBARCHIVE),           # cab
    (b"\x1f\x8b", _ARCH_LIBARCHIVE),       # gzip
    (b"BZh", _ARCH_LIBARCHIVE),            # bzip2
)

# Formats whose signature sits at a fixed offset rather than the head.
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"
_ISO_MAGIC_OFFSET = 0x8001
_ISO_MAGIC = b"CD001"

# Ordered by preference. ``bsdtar`` is in the SteamOS base image and handles
# every format below; the other two are fallbacks for hosts where libarchive
# was built without a codec, and ``unrar`` for rar specifically.
_EXTRACTORS: tuple[str, ...] = ("bsdtar", "7z", "unrar")

_SFX_SCAN_BYTES = 512 * 1024


def mkdir_p(path: Path) -> None:
    """``mkdir -p``, as a named function so it can go to a thread."""
    path.mkdir(parents=True, exist_ok=True)


def available_extractors() -> tuple[str, ...]:
    """Which of :data:`_EXTRACTORS` are on PATH, in preference order.

    Used at connect time to tell the user up front that a format is
    unreachable on this device, rather than after a download or at the end of
    a long extract.
    """
    return tuple(tool for tool in _EXTRACTORS if shutil.which(tool))


def detect_format(path: Path) -> str | None:
    """Detect archive format from magic bytes."""
    try:
        with path.open("rb") as fh:
            header = fh.read(8)
    except Exception:
        return None

    if header[:2] == b"PK":
        return _ARCH_ZIP
    if header[:3] == b"Rar":
        return _ARCH_RAR
    if header[:6] == b"7z\xbc\xaf'\x1c":
        return _ARCH_7Z
    for magic, fmt in _HEAD_MAGIC:
        if header.startswith(magic):
            return fmt
    # tar and iso carry their signature at a fixed offset, so they are only
    # ruled out after the cheap head comparisons above have all missed.
    if _magic_at(path, _TAR_MAGIC_OFFSET, _TAR_MAGIC):
        return _ARCH_LIBARCHIVE
    if _magic_at(path, _ISO_MAGIC_OFFSET, _ISO_MAGIC):
        return _ARCH_LIBARCHIVE
    # 7z stored inside an SFX stub: scan the first 512 KB.
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_SFX_SCAN_BYTES)
        if b"7z\xbc\xaf'\x1c" in chunk:
            return _ARCH_7Z
    except OSError as exc:
        logger.warning(
            "[GameVault archive] could not scan %s for an SFX signature: %s",
            path.name, exc,
        )
    return None


def _magic_at(path: Path, offset: int, magic: bytes) -> bool:
    """Whether *magic* sits at *offset*. A short file simply does not match."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read(len(magic)) == magic
    except OSError:
        return False


async def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack *archive* into *dest*, dispatching on its magic bytes.

    Raises on an unknown or unsupported format so the caller's single failure
    path reports it, rather than each branch building its own
    ``InstallResult``.
    """
    fmt = detect_format(archive)
    if fmt is None:
        raise RuntimeError(f"Unknown archive format: {archive.name}")
    await asyncio.to_thread(mkdir_p, dest)
    if fmt == _ARCH_ZIP:
        await _extract_zip_interruptible(archive, dest)
        return
    await _extract_with_tool(archive, dest, fmt)


async def _extract_zip_interruptible(archive: Path, dest: Path) -> None:
    """Unpack a zip in a worker thread that cancellation can actually stop.

    A plain ``await asyncio.to_thread(...)`` cannot be interrupted: the
    executor future refuses cancellation once the thread is running, so the
    task only sees ``CancelledError`` *after* the whole archive has been
    written — for a large game, minutes of ignoring the user's Cancel while
    the extract runs to completion.

    ``shield`` lets this coroutine take the cancellation immediately, so the
    stop flag is set while the thread is still between members. The inner
    task is then awaited so the thread is not left running loose behind a
    returned coroutine, which costs at most one member's write.
    """
    stop = threading.Event()
    task = asyncio.ensure_future(
        asyncio.to_thread(_extract_zip, archive, dest, stop),
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        with contextlib.suppress(BaseException):
            await task
        raise


def _extract_zip(archive: Path, dest: Path, stop: threading.Event) -> None:
    """Member-by-member so *stop* is checked between files.

    ``extract`` applies the same path sanitising as ``extractall`` — the
    per-member loop is only about the cancellation checkpoint.
    """
    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            if stop.is_set():
                logger.info(
                    "%s zip extraction stopped early: %s", _LOG_PREFIX,
                    archive.name,
                )
                return
            zf.extract(member, str(dest))


def _command_for(tool: str, archive: Path, dest: Path) -> list[str]:
    if tool == "bsdtar":
        return ["bsdtar", "-xf", str(archive), "-C", str(dest)]
    if tool == "unrar":
        return ["unrar", "x", "-y", str(archive), str(dest) + "/"]
    return ["7z", "x", str(archive), f"-o{dest}", "-y"]


async def _extract_with_tool(archive: Path, dest: Path, fmt: str) -> None:
    """Try each available extractor in turn until one succeeds."""
    tried: list[str] = []
    for tool in _EXTRACTORS:
        if tool == "unrar" and fmt != _ARCH_RAR:
            continue
        if not shutil.which(tool):
            continue
        tried.append(tool)
        proc = await asyncio.create_subprocess_exec(
            *_command_for(tool, archive, dest),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate()
        except BaseException:
            # Cancelling this coroutine only unwinds *us*; the extractor
            # keeps running and keeps writing into the install directory
            # that the caller is about to delete. Kill the tree first, then
            # let the cancellation continue.
            await terminate_process_tree(proc, _LOG_PREFIX)
            raise
        if proc.returncode == 0:
            return
        logger.warning(
            "%s %s failed on %s: %s",
            _LOG_PREFIX, tool, archive.name,
            stderr.decode(errors="replace")[:200],
        )

    if not tried:
        raise RuntimeError(
            f"No tool available to extract {fmt} "
            f"(looked for: {', '.join(_EXTRACTORS)})",
        )
    raise RuntimeError(
        f"Could not extract {archive.name} ({fmt}); tried {', '.join(tried)}",
    )
