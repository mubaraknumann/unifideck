"""launcher/proton/infrastructure/proton_integrity.py — is a Proton tree still the one GE shipped?

``ge_installer.proton_install_problem`` answers "is the layout complete":
script, Wine loader, version, manifest. A tree can pass all of that with
its Windows DLLs gutted. Field bundles showed every launch dying within
seconds on ``unimplemented function shell32.dll.SHGetFolderPathW`` —
a real export of a healthy build, so the loaded ``shell32.dll`` was not
the builtin any more. A prefix's ``system32`` entries are symlinks into
the shared Proton tree, so a Wine prefix update writes straight through
them and replaces the shared copy for every game at once.

Three answers live here, by cost:

* :func:`sentinel_dll_problem` — the core builtins still carry Wine's
  ``Wine builtin DLL`` signature (a Wine *fake* DLL carries
  ``Wine placeholder DLL`` and exports nothing). A dozen 80-byte reads.
* :func:`manifest_problem` — every file GE shipped is still present at
  its shipped size (``deep=False``, one ``lstat`` each), or still has its
  shipped SHA-256 (``deep=True``, ~4-8 s for a 1.6 GB tree). The manifest is
  written at install time from a tarball whose SHA-512 matched GE's
  published one, so it records the release, not whatever is on disk later.
* :func:`damaged_marker` — a verdict already reached after a failed run,
  so the next launch routes around the tree with one ``stat``.

None of this runs on a normal launch. The checks run after a run fails
and from the plugin's startup audit; see ``proton_health`` and
``ge_repair``. Stdlib only: the launcher imports it under system python.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_NAME = ".unifideck-manifest.json"
DAMAGED_NAME = ".unifideck-damaged"
MANIFEST_FORMAT = 1

# The builtins the field reports named, and the ones every Windows process
# loads. A gutted copy of any of them fails every launch.
SENTINEL_DLLS = ("ntdll", "kernelbase", "kernel32", "win32u", "user32", "shell32")
_SIGNATURE_OFFSET = 0x40
_BUILTIN = b"Wine builtin DLL"
_PLACEHOLDER = b"Wine placeholder DLL"

# Proton has shipped its 64-bit PE DLLs under both ``lib`` and ``lib64``.
_PE_DIRS = (
    ("x86_64", ("files/lib/wine/x86_64-windows", "files/lib64/wine/x86_64-windows")),
    ("i386", ("files/lib/wine/i386-windows",)),
)

# Our own bookkeeping inside the tree is never part of what GE shipped.
_OWN_FILES = frozenset({MANIFEST_NAME, DAMAGED_NAME})


def _pe_dirs(root: Path) -> list[Path]:
    """The wine PE DLL dirs present under ``root``, at most one per arch."""
    found: list[Path] = []
    for _arch, candidates in _PE_DIRS:
        for rel in candidates:
            if (root / rel).is_dir():
                found.append(root / rel)
                break
    return found


def sentinel_dll_problem(root: Path) -> str | None:
    """Name the first core builtin DLL that is no longer Wine's builtin.

    A sentinel that is absent is ignored, and so is a tree with no wine PE
    dir at all: an unfamiliar layout must never fail this check, only a
    replaced file. ``crypt32`` is deliberately not a sentinel, because a
    game fix patches it in place.
    """
    for pe_dir in _pe_dirs(root):
        arch = pe_dir.name
        for name in SENTINEL_DLLS:
            dll = pe_dir / f"{name}.dll"
            try:
                if not dll.is_file():
                    continue
                with dll.open("rb") as f:
                    f.seek(_SIGNATURE_OFFSET)
                    head = f.read(len(_PLACEHOLDER))
            except OSError as e:
                return f"{arch}/{name}.dll unreadable ({e})"
            if head.startswith(_BUILTIN):
                continue
            if head.startswith(_PLACEHOLDER):
                return f"{arch}/{name}.dll is a Wine placeholder, not the builtin"
            return f"{arch}/{name}.dll is truncated or replaced"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(
    root: Path,
    path: Path,
    files: dict[str, list[Any]],
    links: dict[str, str],
    *,
    is_file: bool,
) -> None:
    """Add one walked entry to the manifest maps. Raises ``OSError``."""
    rel = path.relative_to(root).as_posix()
    if path.is_symlink():
        links[rel] = os.readlink(path)
    elif is_file and rel not in _OWN_FILES:
        files[rel] = [path.stat().st_size, _sha256(path)]


def build_manifest(root: Path, tag: str, tarball_sha512: str) -> dict[str, Any] | None:
    """Record every file and symlink under a freshly extracted ``root``.

    Called only on a tree just unpacked from a verified tarball into a
    private staging dir, so what it records is the release. Returns None if
    the walk fails, and the install then carries no manifest (quick checks
    still run).
    """
    files: dict[str, list[Any]] = {}
    links: dict[str, str] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            for name in dirnames:  # a symlinked dir; os.walk does not descend it
                _record(root, base / name, files, links, is_file=False)
            for name in filenames:
                _record(root, base / name, files, links, is_file=True)
    except OSError as e:
        logger.warning("[proton_integrity] could not build manifest for %s: %s", root, e)
        return None
    return {
        "format": MANIFEST_FORMAT,
        "tag": tag,
        "tarball_sha512": tarball_sha512,
        "files": files,
        "links": links,
    }


def load_manifest(root: Path) -> dict[str, Any] | None:
    """The manifest recorded in ``root``, or None if absent or unusable."""
    try:
        data = json.loads((root / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        return None
    if not isinstance(data.get("files"), dict) or not isinstance(data.get("links"), dict):
        return None
    return data


def _write_atomic(path: Path, text: str) -> bool:
    """Write via a unique tmp file + rename, so a reader never sees half."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    except OSError as e:
        logger.warning("[proton_integrity] could not write %s: %s", path, e)
        tmp.unlink(missing_ok=True)
        return False
    return True


def write_manifest(root: Path, manifest: dict[str, Any]) -> bool:
    """Store ``manifest`` in ``root``. True on success."""
    return _write_atomic(root / MANIFEST_NAME, json.dumps(manifest))


def _file_problem(path: Path, size: int, sha: str, *, deep: bool) -> str | None:
    """Why one shipped file no longer matches, or None."""
    try:
        if path.is_symlink() or not path.is_file():
            return "missing"
        actual = path.stat().st_size
        if actual != size:
            return f"size {actual}, expected {size}"
        if deep and _sha256(path) != sha:
            return "content changed"
    except OSError as e:
        return f"unreadable ({e})"
    return None


def _link_problem(path: Path, target: str) -> str | None:
    """Why one shipped symlink no longer matches, or None."""
    try:
        if not path.is_symlink():
            return "missing" if not path.exists() else "no longer a symlink"
        actual = os.readlink(path)
    except OSError as e:
        return f"unreadable ({e})"
    return None if actual == target else f"points at {actual}, expected {target}"


def manifest_problem(root: Path, manifest: dict[str, Any], *, deep: bool) -> str | None:
    """The first shipped file that is missing or altered, plus how many more.

    Files GE did not ship (``pfx/``, ``dist.lock``, ``__pycache__``) are
    ignored: only something *missing or changed* from the release counts.
    """
    problems: list[str] = []
    for rel, (size, sha) in manifest["files"].items():
        why = _file_problem(root / rel, size, sha, deep=deep)
        if why:
            problems.append(f"{rel}: {why}")
    for rel, target in manifest["links"].items():
        why = _link_problem(root / rel, target)
        if why:
            problems.append(f"{rel}: {why}")
    if not problems:
        return None
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    return problems[0] + more


def quick_problem(root: Path) -> str | None:
    """Sentinel DLLs, then every shipped file's presence and size."""
    problem = sentinel_dll_problem(root)
    if problem:
        return problem
    manifest = load_manifest(root)
    return manifest_problem(root, manifest, deep=False) if manifest else None


def deep_problem(root: Path) -> str | None:
    """Sentinel DLLs, then every shipped file's content."""
    problem = sentinel_dll_problem(root)
    if problem:
        return problem
    manifest = load_manifest(root)
    return manifest_problem(root, manifest, deep=True) if manifest else None


def damaged_marker(root: Path) -> str | None:
    """The damage recorded for ``root`` by an earlier check, or None."""
    marker = root / DAMAGED_NAME
    try:
        if not marker.is_file():
            return None
        return marker.read_text().strip() or "marked damaged"
    except OSError:
        return "marked damaged"


def mark_damaged(root: Path, problem: str) -> None:
    """Record ``problem`` so every launch routes around ``root`` until repaired."""
    _write_atomic(root / DAMAGED_NAME, problem + "\n")


def clear_damaged(root: Path) -> None:
    """Drop the damage marker (best effort)."""
    try:
        (root / DAMAGED_NAME).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("[proton_integrity] could not clear marker in %s: %s", root, e)
