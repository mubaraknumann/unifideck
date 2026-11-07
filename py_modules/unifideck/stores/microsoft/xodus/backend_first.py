"""stores/microsoft/xodus/backend_first.py — the backend, ahead of the runtime.

The Xbox PC sign-in only runs ``xodus-cli``, about 10 MB. The runtime next to
it is about 590 MB and can take many minutes. Waiting for both made the sign-in
window open long after the user turned the feature on, when nobody expected it.

So an install gets the backend first and records it here, and the sign-in can
start while the runtime downloads. ``active.json`` keeps its meaning: it names
both components, and launches and downloads still need it. This record is only
read when there is no ``active.json``, and the full install removes it.

Blocking: every function runs on the install thread or through ``to_thread``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unifideck.launcher.proton.infrastructure.file_lock import flock_path
from unifideck.launcher.xbox_pc.backend import check_backend
from unifideck.launcher.xbox_pc.layout import (
    STAGING_PREFIX,
    backend_root,
    data_root,
    ensure_private_dir,
    install_lock_path,
    read_active,
    read_json_dict,
)

from .release_source import ReleaseSource
from .runtime_source import BackendListing, LocalDirSource

logger = logging.getLogger(__name__)

RECORD_NAME = "backend_only.json"
#: ``progress`` stage sent once the sign-in can run.
READY_STAGE = "backend_ready"
_LOCK_WAIT_S = 30.0

ProgressFn = Callable[[str, int, "int | None"], None]
#: ``runtime.stage_backend``, passed in because ``runtime`` imports this module.
StageFn = Callable[[BackendListing, Path, threading.Event], None]


@dataclass(frozen=True)
class BackendOnly:
    """A verified-by-manifest backend with no runtime beside it yet."""

    backend_version: str
    backend_dir: Path
    manifest_sha256: str


def _record_path() -> Path:
    return data_root() / RECORD_NAME


def read_record() -> BackendOnly | None:
    """The recorded backend, or None. Never raises for a bad file."""
    data = read_json_dict(_record_path())
    if data is None:
        return None
    version, name, digest = data.get("version"), data.get("dir"), data.get("manifest_sha256")
    if not all(isinstance(v, str) and v for v in (version, name, digest)):
        return None
    directory = backend_root() / str(name)
    if directory.parent != backend_root() or str(name) in (".", "..") or not directory.is_dir():
        return None
    return BackendOnly(str(version), directory, str(digest))


def clear_record() -> None:
    """Forget the record. The full install has taken over."""
    _record_path().unlink(missing_ok=True)


def _write_record(record: BackendOnly) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".backend-only-", dir=data_root())
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({
            "version": record.backend_version, "dir": record.backend_dir.name,
            "manifest_sha256": record.manifest_sha256,
        }, fh)
    os.replace(tmp, _record_path())


def _listing(
    source: LocalDirSource | ReleaseSource, progress: ProgressFn, abort: threading.Event,
) -> BackendListing:
    if isinstance(source, ReleaseSource):
        return source.fetch_backend(source.offer(), progress, abort)
    return source.describe_backend()


def _promote(staged: Path, listing: BackendListing) -> bool:
    with flock_path(install_lock_path(), timeout_s=_LOCK_WAIT_S) as held:
        if not held:
            return False
        ensure_private_dir(backend_root())
        target = backend_root() / f"{listing.version}-{listing.manifest_sha256[:8]}"
        n = 1
        while target.exists() or target.is_symlink():
            target = backend_root() / f"{listing.version}-{listing.manifest_sha256[:8]}.{n}"
            n += 1
        staged.rename(target)
        _write_record(BackendOnly(listing.version, target, listing.manifest_sha256))
    return True


def ensure_backend(
    source: LocalDirSource | ReleaseSource, progress: ProgressFn, abort: threading.Event, stage: StageFn,
) -> None:
    """Make a usable backend available before the runtime starts.

    Does nothing when ``active.json`` already names one. Raises ``SourceError``
    like the full install does; a busy install lock only delays the sign-in to
    the end of the install, so it is logged and the install carries on.
    """
    current = read_active()
    if current is not None and check_backend(current)[1] is None:
        return
    record = read_record()
    if record is not None and check_backend(record)[1] is None:
        progress(READY_STAGE, 0, None)
        return
    listing = _listing(source, progress, abort)
    ensure_private_dir(data_root())
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=data_root()))
    try:
        stage(listing, staging / "backend", abort)
        if not _promote(staging / "backend", listing):
            logger.warning(
                "[XboxPcRuntime] the install lock is busy; the Xbox PC sign-in waits "
                "for the whole install instead of starting early",
            )
            return
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    logger.info("[XboxPcRuntime] backend %s ready, ahead of the runtime", listing.version)
    progress(READY_STAGE, 0, None)
