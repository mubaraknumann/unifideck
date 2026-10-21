"""stores/microsoft/xodus/runtime.py — install the Xodus backend and runtime.

One install copies the backend binaries and unpacks the runtime archive
(349 MB packed, 1.5 GB unpacked) from a ``runtime_source`` folder::

    1. describe the source; stop if those exact components are active
    2. make the data root private, then check free space
    3. stage into ``.staging-*`` with NO lock held, so launches never wait
    4. copy the manifest's files, checking each SHA-256
    5. check the archive's SHA-512, then unpack it (``filter="data"``),
       hashing the bytes tarfile reads too: a file rewritten in between
       fails the second check
    6. check the runtime: proton, wine, wineserver and its metadata file
    7. promote under a short exclusive ``install.lock``: re-check, sweep
       stale staging, rename, write ``active.json``, then GC old folders

The work runs in one thread with an abort ``Event`` that every copy chunk,
hash chunk and archive member checks. A second ``install`` while one runs
waits for the same result. Missing runtime features do not fail the
install; they are recorded as ``degraded`` and shown to the user.

The runtime stays writable: its ``proton`` script writes ``__pycache__``.
It never goes into ``compatibilitytools.d``, where Steam would list it.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import lzma
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from unifideck.launcher.proton.infrastructure.file_lock import flock_path
from unifideck.launcher.proton.infrastructure.ge_installer import proton_install_problem
from unifideck.launcher.xbox_pc.backend import check_backend
from unifideck.launcher.xbox_pc.component_locks import gc_unreferenced, sweep_staging
from unifideck.launcher.xbox_pc.layout import (
    BACKEND_MANIFEST_NAME,
    CLI_NAME,
    RUNTIME_META_NAME,
    SERVICE_NAME,
    STAGING_PREFIX,
    ActiveComponents,
    backend_root,
    data_root,
    ensure_private_dir,
    install_lock_path,
    read_active,
    read_json_dict,
    runtime_root,
    write_active,
)
from unifideck.services.download.validators import validate_path

from .runtime_source import LocalDirSource, SourceError, SourceListing

logger = logging.getLogger(__name__)

#: ``progress(stage, done_bytes, total_bytes)``. Called from the worker thread.
ProgressFn = Callable[[str, int, "int | None"], None]

_CHUNK = 1 << 20
#: Report extract progress at most every this many compressed bytes.
_REPORT_EVERY = 4 << 20
#: Unpacked size is unknown, so ask for five times the archive plus this.
SPACE_MARGIN_BYTES = 512 << 20
SPACE_FACTOR = 5
PROMOTE_LOCK_WAIT_S = 60.0
SHUTDOWN_WAIT_S = 10.0
_EXECUTABLES = frozenset({CLI_NAME, SERVICE_NAME})
#: Runtime feature -> what the user loses without it (status ``degraded``).
DEGRADED_FEATURES: tuple[tuple[str, str], ...] = (
    ("cloud-saves-pr28", "cloud_saves"),
    ("xodus-tcp-bridge-v1", "xbox_sign_in_in_game"),
)
_NUMBERS_RE = re.compile(r"\d+")
#: Python 3.11.4 and later can unpack with ``filter="data"``.
HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


class ComponentInstallError(Exception):
    """The install failed. ``code`` is shown to the user."""

    def __init__(self, code: str) -> None:
        """``code`` is like ``runtime_checksum_mismatch`` or ``source_missing:<path>``."""
        super().__init__(code)
        self.code = code


def _check_abort(abort: threading.Event) -> None:
    if abort.is_set():
        raise ComponentInstallError("cancelled")


def _version_tuple(text: str | None) -> tuple[int, ...]:
    return tuple(int(n) for n in _NUMBERS_RE.findall(text or ""))


def _same_components(active: ActiveComponents | None, listing: SourceListing) -> bool:
    """True when ``active`` is exactly what the source offers and is intact."""
    if active is None:
        return False
    if active.manifest_sha256 != listing.backend.manifest_sha256:
        return False
    if active.archive_sha512 != listing.runtime.sha512:
        return False
    return check_backend(active)[1] is None


def needs_install(listing: SourceListing) -> bool:
    """Whether the active components differ from ``listing``. Blocking."""
    return not _same_components(read_active(), listing)


# ── Copy and hash ──────────────────────────────────────────────────────

def _copy_checked(src: Path, dest: Path, expected: str, abort: threading.Event) -> None:
    """Copy ``src`` to ``dest`` while hashing; refuse a SHA-256 mismatch."""
    digest = hashlib.sha256()
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        while chunk := fin.read(_CHUNK):
            _check_abort(abort)
            digest.update(chunk)
            fout.write(chunk)
    if digest.hexdigest() != expected:
        raise ComponentInstallError(f"backend_checksum_mismatch:{src.name}")


def _sha512_with_progress(path: Path, total: int, progress: ProgressFn, abort: threading.Event) -> str:
    digest = hashlib.sha512()
    done = reported = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            _check_abort(abort)
            digest.update(chunk)
            done += len(chunk)
            if done - reported >= _REPORT_EVERY:
                reported = done
                progress("verifying", done, total)
    return digest.hexdigest()


def stage_backend(listing: SourceListing, dest: Path, abort: threading.Event) -> None:
    """Copy the manifest's files into ``dest`` (a new folder), checked."""
    dest.mkdir(mode=0o755)
    for name, expected in sorted(listing.backend.files.items()):
        target = dest / name
        _copy_checked(listing.backend.directory / name, target, expected, abort)
        target.chmod(0o755 if name in _EXECUTABLES else 0o644)
    (dest / BACKEND_MANIFEST_NAME).write_bytes(listing.backend.manifest_bytes)
    (dest / BACKEND_MANIFEST_NAME).chmod(0o644)


# ── Unpack ─────────────────────────────────────────────────────────────

class _CountingReader:
    """A file wrapper that counts and hashes the compressed bytes tarfile reads.

    The archive was hashed once before. Hashing the very bytes that are
    unpacked closes the gap between that check and the second ``open``.
    """

    def __init__(self, fh: Any, total: int, progress: ProgressFn, abort: threading.Event) -> None:
        self._fh = fh
        self._total = total
        self._progress = progress
        self._abort = abort
        self.done = 0
        self._reported = 0
        self.digest = hashlib.sha512()

    def finish(self) -> str:
        """Read what tarfile left unread and return the SHA-512 of the whole file."""
        while self.read(_CHUNK):
            pass
        return self.digest.hexdigest()

    def read(self, size: int = -1) -> bytes:
        _check_abort(self._abort)
        data: bytes = self._fh.read(size)
        self.digest.update(data)
        self.done += len(data)
        if self.done - self._reported >= _REPORT_EVERY:
            self._reported = self.done
            self._progress("extracting", self.done, self._total)
        return data


def _member_problem(member: tarfile.TarInfo, top: str) -> str | None:
    """Why ``member`` must not be unpacked, or None. Checked on every member."""
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != top:
        return f"runtime_invalid:path {member.name[:80]}"
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        return f"runtime_invalid:type {member.name[:80]}"
    if member.issym() or member.islnk():
        base = path.parent if member.issym() else PurePosixPath()
        target = PurePosixPath(os.path.normpath(str(base / member.linkname)))
        if target.is_absolute() or not target.parts or target.parts[0] != top:
            return f"runtime_invalid:link {member.name[:80]}"
    return None


def _through_link(dest: Path, relative: str) -> bool:
    """Whether ``dest/relative`` resolves through a symlink already unpacked."""
    real_dest = os.path.realpath(dest)
    expected = os.path.normpath(os.path.join(real_dest, relative))
    return os.path.realpath(os.path.join(real_dest, relative)) != expected


def _link_leaves_top(dest: Path, member: tarfile.TarInfo, top: str) -> bool:
    """Whether a symlink's target, resolved on disk, is outside ``dest/top``."""
    real_top = os.path.realpath(dest / top)
    parent = os.path.join(os.path.realpath(dest), str(PurePosixPath(member.name).parent))
    target = os.path.realpath(os.path.join(parent, member.linkname))
    return os.path.commonpath([real_top, target]) != real_top


def _fallback_problem(member: tarfile.TarInfo, dest: Path, top: str) -> bool:
    """Whether the unfiltered unpack of ``member`` could reach outside ``dest``."""
    parent = str(PurePosixPath(member.name).parent)
    if _through_link(dest, parent) or os.path.islink(dest / member.name):
        # Opening, creating or chmod-ing that path would follow the link.
        return True
    if member.islnk() and _through_link(dest, member.linkname):
        return True
    return member.issym() and _link_leaves_top(dest, member, top)


def _fallback_extract(tf: tarfile.TarFile, member: tarfile.TarInfo, dest: Path, top: str) -> None:
    """Unpack one member on Python before 3.11.4, which has no tar filter.

    ``_member_problem`` checks names lexically, which a chain of unpacked
    symlinks can defeat. So, like the ``data`` filter, this checks the real
    filesystem. It refuses a member whose folder resolves through a symlink,
    a member whose own path is already a symlink, a hard link whose target
    resolves through one, and a symlink whose target resolves outside
    ``dest/top``. Nothing is then ever written outside ``dest``. It also
    drops the setuid, setgid, sticky and group or other write bits.
    """
    if _fallback_problem(member, dest, top):
        raise ComponentInstallError(f"runtime_invalid:link {member.name[:80]}")
    member.mode &= 0o755
    tf.extract(member, dest, set_attrs=True)


def _extract_member(tf: tarfile.TarFile, member: tarfile.TarInfo, dest: Path, top: str) -> None:
    if HAS_DATA_FILTER:
        tf.extract(member, dest, set_attrs=True, filter="data")
    else:
        _fallback_extract(tf, member, dest, top)


def _extract_all(tf: tarfile.TarFile, top: str, dest: Path) -> None:
    for member in tf:
        problem = _member_problem(member, top)
        if problem is not None:
            raise ComponentInstallError(problem)
        _extract_member(tf, member, dest, top)


def unpack_runtime(
    listing: SourceListing, dest: Path, progress: ProgressFn, abort: threading.Event,
) -> Path:
    """Unpack the runtime archive into ``dest``. Returns the top folder."""
    archive, top = listing.runtime.archive, listing.runtime.top_dir
    dest.mkdir(mode=0o755)
    with open(archive, "rb") as raw:
        reader: Any = _CountingReader(raw, listing.runtime.size, progress, abort)
        try:
            with tarfile.open(fileobj=reader, mode="r|xz") as tf:
                _extract_all(tf, top, dest)
        except (tarfile.TarError, lzma.LZMAError, EOFError) as e:
            raise ComponentInstallError(f"runtime_invalid:archive {type(e).__name__}") from None
        if reader.finish() != listing.runtime.sha512:
            # The archive changed after it was checked (a rebuild landed).
            raise ComponentInstallError("runtime_checksum_mismatch")
    progress("extracting", listing.runtime.size, listing.runtime.size)
    root = dest / top
    if not root.is_dir() or [p.name for p in dest.iterdir()] != [top]:
        raise ComponentInstallError("runtime_invalid:layout")
    return root


def validate_runtime(root: Path, listing: SourceListing) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Check an unpacked runtime. Returns ``(features, degraded)``."""
    problem = proton_install_problem(root / "proton")
    if problem is not None:
        raise ComponentInstallError(f"runtime_invalid:{problem}")
    if not (root / "files" / "bin" / "wineserver").is_file():
        raise ComponentInstallError("runtime_invalid:files/bin/wineserver missing")
    meta = read_json_dict(root / RUNTIME_META_NAME)
    if meta is None:
        raise ComponentInstallError(f"runtime_invalid:{RUNTIME_META_NAME}")
    required = meta.get("requiresBackendVersion")
    have = listing.backend.upstream_version
    if isinstance(required, str) and _version_tuple(required) > _version_tuple(have):
        raise ComponentInstallError("runtime_backend_incompatible")
    raw_features = meta.get("features")
    features = tuple(f for f in raw_features if isinstance(f, str)) if isinstance(raw_features, list) else ()
    degraded = tuple(label for feature, label in DEGRADED_FEATURES if feature not in features)
    return features, degraded


# ── Promote ────────────────────────────────────────────────────────────

def _free_name(parent: Path, name: str) -> Path:
    """``parent/name``, or ``parent/name.<n>`` when that is taken."""
    candidate, n = parent / name, 1
    while candidate.exists() or candidate.is_symlink():
        candidate, n = parent / f"{name}.{n}", n + 1
    return candidate


def _place(staging: Path, listing: SourceListing) -> tuple[Path, Path]:
    """Rename the staged backend and runtime into free names. Returns both."""
    ensure_private_dir(backend_root())
    ensure_private_dir(runtime_root())
    backend_name = f"{listing.backend.version}-{listing.backend.manifest_sha256[:8]}"
    backend_dir = _free_name(backend_root(), backend_name)
    runtime_dir = _free_name(runtime_root(), listing.runtime.version)
    (staging / "backend").rename(backend_dir)
    (staging / "runtime" / listing.runtime.top_dir).rename(runtime_dir)
    return backend_dir, runtime_dir


def _active_for(
    listing: SourceListing, dirs: tuple[Path, Path], features: tuple[str, ...], degraded: tuple[str, ...],
) -> ActiveComponents:
    return ActiveComponents(
        backend_version=listing.backend.version, backend_dir=dirs[0],
        manifest_sha256=listing.backend.manifest_sha256,
        runtime_version=listing.runtime.version, runtime_dir=dirs[1],
        archive_sha512=listing.runtime.sha512, features=features, degraded=degraded,
        source=listing.source, installed_at=time.time(),
    )


def promote(
    staging: Path, listing: SourceListing, features: tuple[str, ...], degraded: tuple[str, ...],
) -> ActiveComponents:
    """Move the staged folders into place and switch ``active.json``.

    Holds ``install.lock`` exclusively only for this short step.
    """
    with flock_path(install_lock_path(), timeout_s=PROMOTE_LOCK_WAIT_S) as held:
        if not held:
            raise ComponentInstallError("components_busy")
        current = read_active()
        if current is not None and _same_components(current, listing):
            return current
        sweep_staging(keep=staging)
        active = _active_for(listing, _place(staging, listing), features, degraded)
        write_active(active)
        removed = gc_unreferenced(active)
    if removed:
        logger.info("[XboxPcRuntime] removed old components: %s", ", ".join(removed))
    return active


def _already_active(listing: SourceListing) -> ActiveComponents | None:
    current = read_active()
    return current if current is not None and _same_components(current, listing) else None


def _new_staging() -> Path:
    """A private ``.staging-*`` folder on the same filesystem as the root."""
    return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=data_root()))


def _space_check(listing: SourceListing) -> None:
    """Make the root private first: ``validate_path`` would create it 0755."""
    ensure_private_dir(data_root())
    need = listing.runtime.size * SPACE_FACTOR + SPACE_MARGIN_BYTES
    need += sum((listing.backend.directory / n).stat().st_size for n in listing.backend.files)
    result = validate_path(str(data_root()), required_bytes=need)
    if not result.success:
        raise ComponentInstallError(result.error or "insufficient_space")


def _describe(source: LocalDirSource) -> SourceListing:
    try:
        return source.describe()
    except SourceError as e:
        raise ComponentInstallError(e.code) from None


def _stage(
    listing: SourceListing, staging: Path, progress: ProgressFn, abort: threading.Event,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Fill ``staging`` with a checked backend and runtime. No lock is held."""
    progress("copying_backend", 0, None)
    stage_backend(listing, staging / "backend", abort)
    progress("verifying", 0, listing.runtime.size)
    digest = _sha512_with_progress(listing.runtime.archive, listing.runtime.size, progress, abort)
    if digest != listing.runtime.sha512:
        raise ComponentInstallError("runtime_checksum_mismatch")
    progress("extracting", 0, listing.runtime.size)
    root = unpack_runtime(listing, staging / "runtime", progress, abort)
    progress("finalizing", 0, None)
    return validate_runtime(root, listing)


def install_sync(source: LocalDirSource, progress: ProgressFn, abort: threading.Event) -> ActiveComponents:
    """The whole install, blocking. Raises ``ComponentInstallError``."""
    progress("checking", 0, None)
    listing = _describe(source)
    current = _already_active(listing)
    if current is not None:
        return current
    _space_check(listing)
    staging = _new_staging()
    try:
        features, degraded = _stage(listing, staging, progress, abort)
        _check_abort(abort)
        return promote(staging, listing, features, degraded)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class ComponentInstaller:
    """Single-flight runner of ``install_sync`` in a worker thread."""

    def __init__(self, source_fn: Callable[[], LocalDirSource]) -> None:
        """``source_fn`` reads the configured source (it may raise ``SourceError``)."""
        self._source_fn = source_fn
        self._task: asyncio.Task[ActiveComponents] | None = None
        self._abort = threading.Event()

    @property
    def running(self) -> bool:
        """Whether an install is in progress."""
        return self._task is not None and not self._task.done()

    async def install(self, progress: ProgressFn) -> ActiveComponents:
        """Install, or wait for the install already running."""
        if self._task is None or self._task.done():
            self._abort = threading.Event()
            self._task = asyncio.create_task(self._run(progress), name="xbox-pc-components")
        return await asyncio.shield(self._task)

    async def _run(self, progress: ProgressFn) -> ActiveComponents:
        try:
            source = self._source_fn()
        except SourceError as e:
            raise ComponentInstallError(e.code) from None
        try:
            return await asyncio.to_thread(install_sync, source, progress, self._abort)
        except ComponentInstallError:
            raise
        except OSError as e:
            logger.exception("[XboxPcRuntime] install failed")
            raise ComponentInstallError(f"install_io_error:{type(e).__name__}") from None

    def cancel(self) -> None:
        """Ask a running install to stop at its next chunk."""
        self._abort.set()

    async def shutdown(self) -> None:
        """Stop a running install and wait for its thread (bounded)."""
        task = self._task
        if task is None or task.done():
            return
        self._abort.set()
        done, _ = await asyncio.wait({task}, timeout=SHUTDOWN_WAIT_S)
        if task in done:
            with contextlib.suppress(BaseException):
                task.result()
        else:
            logger.warning("[XboxPcRuntime] install thread still running after %.0fs", SHUTDOWN_WAIT_S)
