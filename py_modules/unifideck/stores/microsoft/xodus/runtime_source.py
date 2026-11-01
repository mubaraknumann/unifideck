"""stores/microsoft/xodus/runtime_source.py — where the components come from.

By default the components come from a GitHub release
(``release_source.py``). A folder path in ``stores.microsoft.xbox_pc.source``
installs from the ``out/`` of the build repo instead, for development::

    out/backend/   xodus-cli  xodus-service  LICENSE  manifest.json
                   (plus a -source.tar.xz we never read)
    out/runtime/   unifideck-xodus-runtime-<ver>-x86_64.tar.xz
                   unifideck-xodus-runtime-<ver>-x86_64.sha512sum
                   (plus -source.* siblings we never read)

Only the names in ``manifest.json["files"]`` are copied from the backend
folder, and only the exact ``-x86_64`` runtime archive is used. Any other
``https://`` source value is refused.

Every error names where it looked, so the QAM can say so (rule 8).
Describing a source reads two small files and hashes nothing, so it is
cheap enough for every status refresh. It is still blocking file I/O:
call it through ``asyncio.to_thread``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.xbox_pc.layout import BACKEND_MANIFEST_NAME, CLI_NAME, SERVICE_NAME

if TYPE_CHECKING:
    from .release_source import ReleaseSource

RUNTIME_PREFIX = "unifideck-xodus-runtime-"
RUNTIME_SUFFIX = "-x86_64.tar.xz"
CHECKSUM_SUFFIX = "-x86_64.sha512sum"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_SHA512_RE = re.compile(r"^[0-9a-f]{128}\Z")
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}\Z")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class SourceError(Exception):
    """The source cannot be used. ``code`` is shown to the user."""

    def __init__(self, code: str) -> None:
        """``code`` looks like ``source_missing:<path>``."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BackendListing:
    """The backend files a source offers."""

    directory: Path
    version: str
    manifest_bytes: bytes
    manifest_sha256: str
    #: ``name -> sha256``, as listed in the manifest.
    files: dict[str, str]
    #: ``imported.backendVersion``: the upstream backend it is built from.
    upstream_version: str | None


@dataclass(frozen=True)
class RuntimeListing:
    """The runtime archive a source offers."""

    archive: Path
    size: int
    sha512: str
    version: str
    #: The one folder at the top of the archive.
    top_dir: str


@dataclass(frozen=True)
class SourceListing:
    """Everything one install needs to know about a source."""

    backend: BackendListing
    runtime: RuntimeListing
    #: The source folder as the user would recognise it.
    source: str


def _manifest_files(manifest: dict[str, Any]) -> dict[str, str]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise SourceError("source_incomplete:backend/manifest.json")
    clean: dict[str, str] = {}
    for name, digest in files.items():
        if not isinstance(name, str) or not _FILE_NAME_RE.match(name) or name == BACKEND_MANIFEST_NAME:
            raise SourceError(f"source_incomplete:backend/{name}")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise SourceError(f"source_incomplete:backend/{name}")
        clean[name] = digest
    if CLI_NAME not in clean or SERVICE_NAME not in clean:
        raise SourceError("source_incomplete:backend/manifest.json")
    return clean


def _read_backend(directory: Path) -> BackendListing:
    manifest_path = directory / BACKEND_MANIFEST_NAME
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        raise SourceError(f"source_incomplete:{manifest_path}") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise SourceError(f"source_incomplete:{manifest_path}")
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise SourceError(f"source_incomplete:{manifest_path}")
    files = _manifest_files(manifest)
    for name in files:
        if not (directory / name).is_file():
            raise SourceError(f"source_incomplete:{directory / name}")
    imported = manifest.get("imported")
    upstream = imported.get("backendVersion") if isinstance(imported, dict) else None
    return BackendListing(
        directory=directory,
        version=version,
        manifest_bytes=raw,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        files=files,
        upstream_version=upstream if isinstance(upstream, str) else None,
    )


def _checksum_for(archive: Path) -> str:
    """The SHA-512 that ``<stem>-x86_64.sha512sum`` lists for ``archive``."""
    sums = archive.with_name(archive.name[: -len(RUNTIME_SUFFIX)] + CHECKSUM_SUFFIX)
    try:
        lines = sums.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise SourceError(f"source_incomplete:{sums}") from None
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == archive.name and _SHA512_RE.match(parts[0]):
            return parts[0]
    raise SourceError(f"source_incomplete:{sums}")


def _read_runtime(directory: Path) -> RuntimeListing:
    found = sorted(
        p for p in directory.glob(f"{RUNTIME_PREFIX}*{RUNTIME_SUFFIX}")
        if p.is_file() and not p.name.endswith(f"-source{RUNTIME_SUFFIX}")
    )
    if not found:
        raise SourceError(f"source_incomplete:{directory / (RUNTIME_PREFIX + '*' + RUNTIME_SUFFIX)}")
    if len(found) > 1:
        raise SourceError(f"source_ambiguous:{directory}")
    archive = found[0]
    version = archive.name[len(RUNTIME_PREFIX): -len(RUNTIME_SUFFIX)]
    if not _VERSION_RE.match(version):
        raise SourceError(f"source_incomplete:{archive}")
    return RuntimeListing(
        archive=archive,
        size=archive.stat().st_size,
        sha512=_checksum_for(archive),
        version=version,
        top_dir=f"{RUNTIME_PREFIX}{version}",
    )


class LocalDirSource:
    """A build repo's ``out/`` folder."""

    def __init__(self, root: Path) -> None:
        """``root`` is already expanded."""
        self.root = root
        #: What the QAM names as the place it looked.
        self.where = str(root)

    def describe(self) -> SourceListing:
        """What this source offers, or raise ``SourceError``. Blocking."""
        if not self.root.is_dir():
            raise SourceError(f"source_missing:{self.root}")
        backend_dir, runtime_dir = self.root / "backend", self.root / "runtime"
        for sub in (backend_dir, runtime_dir):
            if not sub.is_dir():
                raise SourceError(f"source_incomplete:{sub}")
        return SourceListing(
            backend=_read_backend(backend_dir),
            runtime=_read_runtime(runtime_dir),
            source=str(self.root),
        )


def source_from_config(value: Any) -> LocalDirSource | ReleaseSource:
    """The source the config names, or raise ``SourceError``.

    Empty or ``github`` is the GitHub release; a path is a local folder.
    """
    from .release_source import ReleaseSource  # it imports this module

    text = value.strip() if isinstance(value, str) else ""
    if not text or text == "github":
        return ReleaseSource()
    if text.startswith(("https://", "http://")):
        raise SourceError("source_unsupported")
    return LocalDirSource(Path(text).expanduser())
