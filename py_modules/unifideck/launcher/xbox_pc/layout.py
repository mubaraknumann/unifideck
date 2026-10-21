"""launcher/xbox_pc/layout.py — the on-disk layout of the Xbox PC feature.

One definition of every path, shared by the plugin (component install,
sign-in, library, game install) and the launcher (game launch)::

    <data>/unifideck/xbox_pc/          0700
      install.lock                     LOCK_EX only to promote or GC (short)
      active.json                      0600, atomic: the installed components
      backend/<ver>-<sha8>/            xodus-cli xodus-service LICENSE manifest.json
      runtime/<ver>/                   writable (proton writes __pycache__)
      locks/                           backend-<dir>.lock, runtime-<dir>.lock,
                                       game-<PID>.lock
      credentials/                     0700, files 0600 (XODUS_CREDENTIAL_DIR)
      state/account.json               0600
      installed/<PID>.json             0600, atomic install records
      .staging-*/                      component installs in progress

``<data>`` is ``$XDG_DATA_HOME`` when it is an absolute path, else
``~/.local/share``. It is read on every call, so a test can point it at a
temporary folder. Game prefixes are NOT under this root: they stay at
``~/.local/share/unifideck/prefixes/<PID>`` (launcher invariant 2, see
``prefix_layout.game_prefix_root``).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The Microsoft store is the one store whose PC titles run through Xodus.
#: Code that branches on this asks :func:`is_xodus_store`, never
#: ``store == "microsoft"``.
XODUS_STORES = frozenset({"microsoft"})

#: Config keys of the feature (``defaults/config.json``).
XBOX_PC_ENABLED_KEY = "stores.microsoft.xbox_pc.enabled"
XBOX_PC_SOURCE_KEY = "stores.microsoft.xbox_pc.source"
XBOX_PC_NOTICE_ACK_KEY = "stores.microsoft.xbox_pc.notice_ack_version"

#: The runtime's metadata file. Upstream may rename it (I-022); this is the
#: one place to change.
RUNTIME_META_NAME = "XODUS_NEXUS_PATCH.json"

#: The backend's checksum manifest, in the source folder and in every
#: installed ``backend/<dir>/``.
BACKEND_MANIFEST_NAME = "manifest.json"
CLI_NAME = "xodus-cli"
SERVICE_NAME = "xodus-service"

#: Prefix of the private ``mkdtemp`` folders a component install stages in.
STAGING_PREFIX = ".staging-"

#: How long a reader waits for ``install.lock`` before giving up.
SHARED_LOCK_WAIT_S = 10.0

#: A Microsoft Store product id. Checked before any path or argv is built.
PID_RE = re.compile(r"^[A-Z0-9]{12}\Z")

ACTIVE_SCHEMA = 1


class UnsafePathError(OSError):
    """A private folder is a symlink or belongs to another user."""


def is_xodus_store(store: str) -> bool:
    """Whether ``store``'s PC titles install and run through Xodus."""
    return store in XODUS_STORES


def validate_pid(pid: str) -> str:
    """Return ``pid`` when it is a product id; raise ``ValueError`` if not."""
    if not isinstance(pid, str) or not PID_RE.match(pid):
        raise ValueError(f"not a Microsoft Store product id: {pid!r}")
    return pid


# ── Paths ────────────────────────────────────────────────────────────────

def data_root() -> Path:
    """``<XDG_DATA_HOME or ~/.local/share>/unifideck/xbox_pc``."""
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".local" / "share"
    return base / "unifideck" / "xbox_pc"


def install_lock_path() -> Path:
    """The lock that serialises component promotion and GC."""
    return data_root() / "install.lock"


def active_path() -> Path:
    """The marker naming the installed components."""
    return data_root() / "active.json"


def backend_root() -> Path:
    """Parent of every installed backend folder."""
    return data_root() / "backend"


def runtime_root() -> Path:
    """Parent of every installed runtime folder."""
    return data_root() / "runtime"


def locks_dir() -> Path:
    """Folder of the per-component and per-game lock files."""
    return data_root() / "locks"


def credential_dir() -> Path:
    """``XODUS_CREDENTIAL_DIR``: always absolute, 0700."""
    return data_root() / "credentials"


def state_dir() -> Path:
    """Small private state files (the verified account)."""
    return data_root() / "state"


def account_state_path() -> Path:
    """``state/account.json``: the verified gamertag."""
    return state_dir() / "account.json"


def installed_dir() -> Path:
    """Folder of the per-game install records."""
    return data_root() / "installed"


def install_root_for(base: str | Path, pid: str) -> Path:
    """``<base>/Xbox/<PID>``: the unit of install, marker and deletion."""
    return Path(base) / "Xbox" / validate_pid(pid)


def game_dir_for(install_root: Path) -> Path:
    """``<installRoot>/game``. Launch views go in its parent, so they are
    swept with the install root."""
    return install_root / "game"


# ── Private files ────────────────────────────────────────────────────────

def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` as a 0700 folder of ours, or fix its mode. Returns it.

    Refuses (``UnsafePathError``) a symlink or a folder that belongs to
    another user, because credentials and install records live under it.
    Missing parents are created with the default mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(f"not a real folder: {path}")
    if st.st_uid != os.getuid():
        raise UnsafePathError(f"folder belongs to uid {st.st_uid}: {path}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def write_private_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` atomically as a 0600 file.

    A temp file in the same folder, fsync, rename, then fsync the folder.
    A reader sees the old file or the new one, never half of either.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    """Make a rename in ``path`` durable. Best effort."""
    with contextlib.suppress(OSError):
        dfd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def read_json_dict(path: Path) -> dict[str, Any] | None:
    """The JSON object in ``path``, or None when missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── active.json ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActiveComponents:
    """The installed backend and runtime, as ``active.json`` names them.

    ``backend_dir`` and ``runtime_dir`` are absolute. On disk they are
    stored relative to :func:`data_root`, so the root can move.
    ``degraded`` lists missing runtime features (for example
    ``cloud_saves``); the UI must show them (CLAUDE.md rule 8).
    """

    backend_version: str
    backend_dir: Path
    manifest_sha256: str
    runtime_version: str
    runtime_dir: Path
    archive_sha512: str
    features: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    source: str = ""
    installed_at: float = 0.0


def _inside_root(rel: Any, parent: Path) -> Path | None:
    """``data_root()/rel`` when ``rel`` names a direct child of ``parent``."""
    if not isinstance(rel, str) or not rel:
        return None
    path = data_root() / rel
    if path.parent != parent or path.name in ("", ".", ".."):
        return None
    return path


def _str_tuple(value: Any) -> tuple[str, ...]:
    """A tuple of the strings in a JSON list (anything else is dropped)."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _parse_active(data: dict[str, Any]) -> ActiveComponents | None:
    """Validate an ``active.json`` object. None when any field is wrong."""
    backend = data.get("backend")
    runtime = data.get("runtime")
    if data.get("schema") != ACTIVE_SCHEMA:
        return None
    if not isinstance(backend, dict) or not isinstance(runtime, dict):
        return None
    backend_dir = _inside_root(backend.get("dir"), backend_root())
    runtime_dir = _inside_root(runtime.get("dir"), runtime_root())
    texts = (
        backend.get("version"), backend.get("manifest_sha256"),
        runtime.get("version"), runtime.get("archive_sha512"),
    )
    if backend_dir is None or runtime_dir is None:
        return None
    if not all(isinstance(t, str) and t for t in texts):
        return None
    installed_at = data.get("installed_at")
    return ActiveComponents(
        backend_version=str(texts[0]),
        backend_dir=backend_dir,
        manifest_sha256=str(texts[1]),
        runtime_version=str(texts[2]),
        runtime_dir=runtime_dir,
        archive_sha512=str(texts[3]),
        features=_str_tuple(runtime.get("features")),
        degraded=_str_tuple(runtime.get("degraded")),
        source=str(data.get("source") or ""),
        installed_at=float(installed_at) if isinstance(installed_at, (int, float)) else 0.0,
    )


def read_active() -> ActiveComponents | None:
    """The installed components, or None.

    None when ``active.json`` is missing or malformed, or when a folder it
    names is gone. It never raises for a bad file: the caller reports
    "components missing" and offers an install.
    """
    data = read_json_dict(active_path())
    if data is None:
        return None
    active = _parse_active(data)
    if active is None:
        return None
    if not active.backend_dir.is_dir() or not active.runtime_dir.is_dir():
        return None
    return active


def write_active(active: ActiveComponents) -> None:
    """Write ``active.json`` atomically (0600). The root must exist.

    Call it under ``install.lock`` held exclusively, after both folders are
    in place.
    """
    root = data_root()
    data = {
        "schema": ACTIVE_SCHEMA,
        "backend": {
            "version": active.backend_version,
            "dir": active.backend_dir.relative_to(root).as_posix(),
            "manifest_sha256": active.manifest_sha256,
        },
        "runtime": {
            "version": active.runtime_version,
            "dir": active.runtime_dir.relative_to(root).as_posix(),
            "archive_sha512": active.archive_sha512,
            "features": list(active.features),
            "degraded": list(active.degraded),
        },
        "source": active.source,
        "installed_at": active.installed_at,
    }
    write_private_json(active_path(), data)
