"""launcher/xbox_pc/backend.py — find the verified Xodus binaries.

The backend is never looked up on ``PATH`` or in ``~/.local/bin``. An
upstream ``xodus-cli`` there cannot read our credential store (I-012), so
picking it would sign the user out with no clear reason. Instead the two
binaries are taken from the installed ``backend/<dir>/`` and checked
against that folder's ``manifest.json``, whose own hash ``active.json``
records. This is how envelope rule 4 (verified binaries) holds for a
backend that is not bundled in the plugin.

Hashing 43 MB takes a moment, so each file's digest is cached for the life
of the process on its ``(inode, size, mtime_ns)``. A changed file misses
the cache and is hashed again.

Stdlib only, Python 3.10 safe. ``hashlib.file_digest`` is 3.11+, so files
are read in chunks.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .layout import BACKEND_MANIFEST_NAME, CLI_NAME, SERVICE_NAME, ActiveComponents

_CHUNK = 1 << 20

#: ``path -> ((st_ino, st_size, st_mtime_ns), sha256 hex)``.
_DIGEST_CACHE: dict[str, tuple[tuple[int, int, int], str]] = {}


@dataclass(frozen=True)
class BackendPaths:
    """The verified binaries of one installed backend."""

    cli: Path
    service: Path
    directory: Path
    version: str


def stream_digest(path: Path, algorithm: str) -> str:
    """Hex digest of ``path``, read in 1 MiB chunks."""
    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 of ``path`` as hex."""
    return stream_digest(path, "sha256")


def sha512_file(path: Path) -> str:
    """SHA-512 of ``path`` as hex."""
    return stream_digest(path, "sha512")


def _cached_sha256(path: Path) -> str | None:
    """SHA-256 of a regular file, cached on its stat identity. None if absent."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    key = (st.st_ino, st.st_size, st.st_mtime_ns)
    hit = _DIGEST_CACHE.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        digest = sha256_file(path)
    except OSError:
        return None
    _DIGEST_CACHE[str(path)] = (key, digest)
    return digest


def _manifest_files(directory: Path, manifest_sha256: str) -> dict[str, str] | str:
    """The manifest's ``files`` map, or a problem code."""
    manifest = directory / BACKEND_MANIFEST_NAME
    try:
        raw = manifest.read_bytes()
    except OSError:
        return "backend_missing"
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        return "backend_tampered"
    try:
        files = json.loads(raw).get("files")
    except (ValueError, AttributeError):
        return "backend_tampered"
    if not isinstance(files, dict) or not {CLI_NAME, SERVICE_NAME} <= set(files):
        return "backend_tampered"
    return {str(k): str(v) for k, v in files.items()}


def _file_problem(path: Path, expected: str, *, executable: bool) -> str | None:
    """Why one manifest file does not check out, or None."""
    if path.is_symlink() or not path.is_file():
        return "backend_missing"
    if _cached_sha256(path) != expected:
        return "backend_tampered"
    if executable and not os.access(path, os.X_OK):
        return "backend_tampered"
    return None


def check_backend(active: ActiveComponents) -> tuple[BackendPaths | None, str | None]:
    """``(paths, None)`` when the backend verifies, else ``(None, problem)``.

    ``problem`` is ``backend_missing`` (a file is gone) or
    ``backend_tampered`` (a hash or the exec bit is wrong). Every file the
    manifest lists is checked, not only the two binaries.
    """
    directory = active.backend_dir
    files = _manifest_files(directory, active.manifest_sha256)
    if isinstance(files, str):
        return None, files
    for name, expected in sorted(files.items()):
        if "/" in name or name in ("", ".", ".."):
            return None, "backend_tampered"
        executable = name in (CLI_NAME, SERVICE_NAME)
        problem = _file_problem(directory / name, expected, executable=executable)
        if problem is not None:
            return None, problem
    return BackendPaths(
        cli=directory / CLI_NAME,
        service=directory / SERVICE_NAME,
        directory=directory,
        version=active.backend_version,
    ), None


def resolve_backend(active: ActiveComponents | None) -> BackendPaths | None:
    """The verified backend binaries, or None. Never searches ``PATH``."""
    if active is None:
        return None
    paths, _problem = check_backend(active)
    return paths
