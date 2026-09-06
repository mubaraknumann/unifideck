"""Async wrappers around standard file operations.

Every public function offloads to ``asyncio.to_thread`` so
calls don't block the event loop. Two flavours:

* **Read-style**     (``exists``, ``is_file``, ``is_dir``,
  ``listdir``, ``stat``, ``read_text``, ``read_json``) —
  errors return safe defaults (``False`` / ``[]`` /
  ``None`` / ``""`` / ``{}``).
* **Write-style**    (``write_text``, ``write_bytes``,
  ``write_json``, ``makedirs``, ``copy``, ``move``,
  ``remove``) — return ``bool`` reporting success/failure.

Write operations use the atomic ``tmp + replace`` pattern so
crashes mid-write never leave a torn file. Mode override
support on ``write_text`` / ``write_json`` lets callers pin
the result to ``0o600`` for sensitive payloads.

Private ``_*_sync`` helpers run inside the thread so the
public async function is just an ``await asyncio.to_thread``
delegate — keeps the async surface readable.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)
PathLike = str | os.PathLike[str]

async def exists(path: PathLike) -> bool:
    """Return whether ``path`` exists (file, dir, or symlink).

    Wraps ``Path.exists()`` on a thread. No error
    handling — ``exists()`` itself returns ``False`` on
    OSError.

    Args:
        path: filesystem path.

    Returns:
        True if it exists.
    """
    return await asyncio.to_thread(
        lambda: Path(path).exists(),
    )

async def is_file(path: PathLike) -> bool:
    """Return whether ``path`` is a regular file.

    Symlinks-to-files count as files. Returns ``False``
    on OSError (broken symlink, permission denied) per
    ``Path.is_file`` semantics.

    Args:
        path: filesystem path.

    Returns:
        True if it's a regular file.
    """
    return await asyncio.to_thread(
        lambda: Path(path).is_file(),
    )

async def is_dir(path: PathLike) -> bool:
    """Return whether ``path`` is a directory (or symlink to one).

    Args:
        path: filesystem path.

    Returns:
        True if it's a directory.
    """
    return await asyncio.to_thread(
        lambda: Path(path).is_dir(),
    )

async def listdir(path: PathLike) -> list[str]:
    """Return the names (not paths) of entries in ``path``.

    Empty list on OSError (logged at WARN) — callers
    typically iterate the result and can handle "no
    entries found" identically to "directory doesn't
    exist", which is convenient.

    Args:
        path: directory path.

    Returns:
        List of entry names (filenames + subdir names),
        in OS-defined order.
    """
    try:
        return await asyncio.to_thread(
            lambda: [p.name for p in Path(path).iterdir()],
        )
    except OSError as e:
        logger.warning(
            "[AsyncFileOps] listdir(%s) failed: %s",
            path,
            e,
        )
        return []

async def stat(path: PathLike) -> os.stat_result | None:
    """Return ``os.stat_result`` or ``None`` on OSError.

    Quiet on failure (no log) — used in hot paths where
    "stat says it's gone" is a normal flow signal, not
    an error.

    Args:
        path: filesystem path.

    Returns:
        The stat result, or ``None`` if unreachable.
    """
    try:
        return await asyncio.to_thread(
            lambda: Path(path).stat(),
        )
    except OSError:
        return None

async def makedirs(path: PathLike, mode: int = 0o755, exist_ok: bool = True) -> bool:
    """Create ``path`` (and parents) with the given mode.

    Args:
        path: directory to create.
        mode: octal permission bits (default ``0o755``).
        exist_ok: ``True`` (default) tolerates existing
            paths. Set ``False`` to raise on collision.

    Returns:
        True on success, False on OSError (logged at
        ERROR).
    """
    try:
        await asyncio.to_thread(
            lambda: Path(path).mkdir(
                mode=mode,
                parents=True,
                exist_ok=exist_ok,
            ),
        )
        return True
    except OSError:
        logger.exception("[AsyncFileOps] makedirs(%s) failed", path)
        return False

async def ensure_dir(path: PathLike) -> bool:
    """Idempotent ``makedirs`` shortcut — never errors on existing dirs.

    Args:
        path: directory to ensure exists.

    Returns:
        True on success.
    """
    return await makedirs(path, exist_ok=True)

async def copy(src: PathLike, dst: PathLike) -> bool:
    """Copy ``src`` to ``dst`` preserving metadata (mtime, mode).

    Wraps ``shutil.copy2`` on a thread. Both ``OSError``
    and the broader ``shutil.Error`` are caught (the
    latter covers same-file copies and other shutil-
    specific failures).

    Args:
        src: source path.
        dst: destination path (file or directory).

    Returns:
        True on success.
    """
    try:
        await asyncio.to_thread(shutil.copy2, src, dst)
        return True
    except (OSError, shutil.Error):
        logger.exception("[AsyncFileOps] copy(%s -> %s) failed", src, dst)
        return False

async def move(src: PathLike, dst: PathLike) -> bool:
    """Move ``src`` to ``dst``, across filesystems if needed.

    Wraps ``shutil.move`` which falls back to copy+delete
    when crossing filesystems. Both src/dst are coerced
    to ``str`` for ``shutil.move`` (which is picky about
    PathLike on some Python versions).

    Args:
        src: source path.
        dst: destination path.

    Returns:
        True on success.
    """
    try:
        await asyncio.to_thread(shutil.move, str(src), str(dst))
        return True
    except (OSError, shutil.Error):
        logger.exception("[AsyncFileOps] move(%s -> %s) failed", src, dst)
        return False

async def remove(path: PathLike) -> bool:
    """Delete a file at ``path``, tolerating missing files.

    Uses ``Path.unlink(missing_ok=True)`` so "file
    doesn't exist" is a success rather than an error.
    Other OSErrors (permission denied, path is a
    directory) log at ERROR and return False.

    Args:
        path: file path.

    Returns:
        True if the file is gone after the call.
    """
    try:
        await asyncio.to_thread(
            lambda: Path(path).unlink(missing_ok=True),
        )
        return True
    except OSError:
        logger.exception("[AsyncFileOps] remove(%s) failed", path)
        return False

async def read_text(path: PathLike, encoding: str = "utf-8") -> str | None:
    """Read ``path`` as text, returning ``None`` on any failure.

    Catches both ``OSError`` (file missing, unreadable)
    and ``UnicodeDecodeError`` (file contains non-UTF-8
    bytes when default encoding is used). Both log at
    WARN.

    Args:
        path: file path.
        encoding: text codec (default ``"utf-8"``).

    Returns:
        File contents as string, or ``None`` on error.
    """
    try:
        return await asyncio.to_thread(lambda: Path(path).read_text(encoding=encoding))
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("[AsyncFileOps] read_text(%s) failed: %s", path, e)
        return None

async def write_text(
    path: PathLike, content: str, encoding: str = "utf-8", mode: int = 0o644
) -> bool:
    """Atomically write ``content`` to ``path`` with the given encoding + mode.

    Delegates to ``_write_text_sync`` on a thread. The
    helper does the ``tmp + replace`` dance + optional
    chmod.

    Args:
        path: target file path.
        content: text content.
        encoding: text codec (default ``"utf-8"``).
        mode: octal file mode. Only chmoded if different
            from default ``0o644`` (avoids unnecessary
            chmod calls).

    Returns:
        True on success.
    """
    return await asyncio.to_thread(_write_text_sync, path, content, encoding, mode)

def _write_text_sync(path: PathLike, content: str, encoding: str, mode: int) -> bool:
    """Sync helper for ``write_text`` — atomic write + cleanup on failure.

    Pipeline:

    1. Ensure parent directory exists.
    2. Write content to ``<path>.tmp``.
    3. ``replace`` over ``path`` (atomic on POSIX).
    4. If non-default mode, ``chmod`` the result.

    On OSError, attempt to unlink the leftover tmp file
    so the next write starts clean. Tmp-unlink failures
    are swallowed (best-effort cleanup).

    Args:
        path / content / encoding / mode: forwarded from
            ``write_text``.

    Returns:
        True on success.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(p)
        if mode != 0o644:
            p.chmod(mode)
        return True
    except OSError:
        logger.exception("[AsyncFileOps] write_text(%s) failed", path)
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        return False

async def write_bytes(path: PathLike, data: bytes) -> bool:
    """Atomically write binary ``data`` to ``path``.

    Args:
        path: target file path.
        data: raw bytes.

    Returns:
        True on success.
    """
    return await asyncio.to_thread(_write_bytes_sync, path, data)

def _write_bytes_sync(path: PathLike, data: bytes) -> bool:
    """Sync helper for ``write_bytes`` — atomic write + cleanup.

    Same pattern as ``_write_text_sync`` but for bytes.
    The tmp filename uses ``with_suffix(suffix + ".tmp")``
    rather than naive ``+ ".tmp"`` to handle paths with
    multiple dots correctly.

    Failure swallows the log entirely (no logger call) —
    callers either get ``False`` or success; the binary
    write path is too hot for verbose logs.

    Args:
        path: target path.
        data: raw bytes.

    Returns:
        True on success.
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        tmp.replace(p)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
        return False

async def read_json(path: PathLike) -> dict[str, Any]:
    """Read + parse a JSON file, returning ``{}`` on any failure.

    Empty dict is the failure default (caller sees a
    valid-but-empty mapping). Callers needing to
    distinguish "missing" from "empty" should use
    ``read_text`` and parse manually.

    Args:
        path: file path.

    Returns:
        Parsed dict, or ``{}`` on missing file / decode
        error / OSError.
    """
    return await asyncio.to_thread(_read_json_sync, path)

def _read_json_sync(path: PathLike) -> dict[str, Any]:
    """Sync helper for ``read_json``.

    Three-arm error policy:

    * Missing file → ``{}`` (no log — this is a normal
      flow signal);
    * Decode / encoding error → ``{}`` + WARN log
      (corrupt JSON is worth surfacing);
    * OSError → ``{}`` + WARN log.

    Args:
        path: file path.

    Returns:
        Parsed dict or empty.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            return cast("dict[str, Any]", json.load(f))
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
    ) as e:
        logger.warning(
            "[AsyncFileOps] failed to read JSON %s: %s",
            path,
            e,
        )
        return {}

async def write_json(
    path: PathLike, data: Any, indent: int = 2, mode: int = 0o644
) -> bool:
    """Atomically serialise ``data`` as JSON and write to ``path``.

    Args:
        path: target file path.
        data: any JSON-serialisable value.
        indent: pretty-print indent. ``2`` (default) for
            human-readable cache/config; pass ``None`` for
            compact one-line output.
        mode: octal file mode; only chmoded if different
            from default.

    Returns:
        True on success.
    """
    return await asyncio.to_thread(_write_json_sync, path, data, indent, mode)

def _write_json_sync(path: PathLike, data: Any, indent: int, mode: int = 0o644) -> bool:
    """Sync helper for ``write_json`` — atomic serialise + write + cleanup.

    Three exception classes caught:

    * ``OSError`` — disk full, permission denied, etc.
    * ``TypeError`` — ``data`` contains non-JSON-
      serialisable values (e.g. ``bytes``, ``set``).
    * ``ValueError`` — circular references in ``data``.

    Cleanup: best-effort unlink of the tmp file on
    failure.

    Args:
        path / data / indent / mode: forwarded from
            ``write_json``.

    Returns:
        True on success.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    try:
        content = json.dumps(
            data,
            ensure_ascii=False,
            indent=indent,
        )
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        if mode != 0o644:
            p.chmod(mode)
        return True
    except (OSError, TypeError, ValueError):
        logger.exception("[AsyncFileOps] write_json(%s) failed", path)
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        return False
