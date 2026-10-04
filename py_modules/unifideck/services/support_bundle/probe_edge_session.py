"""support_bundle/probe_edge_session.py — Did the Xbox sign-in survive?

"Game Pass asks me to log in every time" could not be answered from a bundle.
Xbox Cloud Gaming streams in an Edge kiosk that signs in with the Microsoft
cookies in our own profile (``<data>/edge-auth``), and that whole directory
is denied from the archive because it holds cookies (``deny.py``). So a
report could never show whether the sign-in was saved, saved only for the
session, or never written at all.

This probe records the *shape* of that state and never its contents:

1. **the Microsoft cookies**: host, name, persistent or session-only, expiry
   and last access. Never ``value`` or ``encrypted_value``. A sign-in kept
   with "Stay signed in" leaves persistent ``live.com`` cookies; answering
   "No" leaves session cookies that Edge drops on exit;
2. **the cookie database itself**: present, size, last written;
3. **whether Edge can write the profile**: a Flatpak Edge without ``home``
   (or ``host``) access cannot save anything there;
4. **Edge aborts**: ``FATAL`` lines in ``edge-auth.log``. An abort skips the
   shutdown that writes recent cookies to disk.

Everything is best-effort and read-only. The database is read from a copy so
a running Edge never sees a lock.
"""
from __future__ import annotations

import contextlib
import datetime
import logging
import pwd
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from unifideck.utils.mounts import run_demoted

logger = logging.getLogger(__name__)

#: Same ids as ``auth/edge_browser/detection.py``. Not imported: that module
#: is the browser's, and a probe must not pull the auth layer into the bundle.
_FLATPAK_APP = "com.microsoft.Edge"
_NATIVE_BINS = ("microsoft-edge", "microsoft-edge-stable")

#: The domains Microsoft's consumer sign-in and xbox.com set cookies on.
_MS_HOST_PATTERNS = ("%xbox.com%", "%live.com%", "%microsoft.com%", "%microsoftonline.com%")

_COOKIE_QUERY = (
    "SELECT host_key, name, is_persistent, expires_utc, last_access_utc FROM cookies "
    "WHERE host_key LIKE ? OR host_key LIKE ? OR host_key LIKE ? OR host_key LIKE ? "
    "ORDER BY host_key, name"
)

#: Cookies that hold the sign-in itself. Any one of them, persistent, means a
#: relaunched kiosk can sign in to xbox.com without asking.
SIGN_IN_COOKIES = frozenset({"MSPAuth", "WLSSC", "__Host-MSAAUTHP", "ESTSAUTHPERSISTENT"})

_MAX_COOKIES = 60
_LOG_TAIL_BYTES = 256 * 1024
_MAX_LINE_CHARS = 300
_FLATPAK_TIMEOUT = 15.0

#: Chromium stores times as microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.UTC)


def edge_session_block(data_dir: str | None) -> dict[str, Any]:
    """Describe the Edge profile's Microsoft sign-in without its secrets."""
    if not data_dir:
        return {"error": "data dir unresolved"}
    profile = Path(data_dir) / "edge-auth"
    cookies = _cookie_rows(profile / "Default" / "Cookies")
    return {
        "profile_dir": str(profile),
        "profile_exists": profile.is_dir(),
        "cookies_db": _file_facts(profile / "Default" / "Cookies"),
        "microsoft_session": session_state(cookies),
        "cookies": cookies[:_MAX_COOKIES],
        "browser": _browser_block(Path(data_dir), profile),
        "log": _log_block(Path(data_dir) / "edge-auth.log"),
    }


def session_state(cookies: list[dict[str, Any]]) -> str:
    """``persistent``, ``session_only`` or ``none`` for the sign-in cookies."""
    sign_in = [c for c in cookies if c.get("name") in SIGN_IN_COOKIES]
    if any(c.get("persistent") for c in sign_in):
        return "persistent"
    return "session_only" if sign_in else "none"


def _chrome_time(value: int | None) -> str:
    """A Chromium timestamp as an ISO date, or "" for 0 (no expiry)."""
    if not value:
        return ""
    try:
        moment = _CHROME_EPOCH + datetime.timedelta(microseconds=int(value))
    except (OverflowError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def _file_facts(path: Path) -> dict[str, Any]:
    """Present, size and last-written time of one file."""
    try:
        st = path.stat()
    except OSError:
        return {"exists": False}
    written = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.UTC)
    return {"exists": True, "size": st.st_size, "mtime": written.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _cookie_rows(db: Path) -> list[dict[str, Any]]:
    """Microsoft cookie metadata from a copy of ``db``. Never the values."""
    if not db.is_file():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(db, copy)
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(_COOKIE_QUERY, _MS_HOST_PATTERNS).fetchall()
        finally:
            conn.close()
    return [
        {
            "host": host, "name": name, "persistent": bool(persistent),
            "expires": _chrome_time(expires), "last_access": _chrome_time(accessed),
        }
        for host, name, persistent, expires, accessed in rows
    ]


def _run(owner: Path, argv: list[str]) -> tuple[int | None, str]:
    """``(returncode, stdout)`` run as ``owner``'s uid; ``None`` if it failed.

    The backend may run as root, and a root ``flatpak --user`` query reads
    root's installs, not the desktop user's.
    """
    try:
        st = owner.stat()
    except OSError:
        return None, ""
    proc = run_demoted(argv, st.st_uid, st.st_gid, timeout=_FLATPAK_TIMEOUT)
    if proc is None:
        return None, ""
    return proc.returncode, proc.stdout or ""


def _flatpak_scope(owner: Path) -> str:
    """``user``, ``system`` or "" for the Edge Flatpak."""
    if not shutil.which("flatpak"):
        return ""
    for scope in ("user", "system"):
        rc, _ = _run(owner, ["flatpak", "info", f"--{scope}", "--show-ref", _FLATPAK_APP])
        if rc == 0:
            return scope
    return ""


def _filesystems(permissions: str) -> list[str]:
    """The ``filesystems=`` entries of ``flatpak info --show-permissions``."""
    for line in permissions.splitlines():
        if line.startswith("filesystems="):
            return [entry for entry in line.split("=", 1)[1].split(";") if entry]
    return []


def _owner_home(owner: Path) -> str:
    """The home directory of ``owner``'s uid, not the backend's (maybe root)."""
    try:
        return pwd.getpwuid(owner.stat().st_uid).pw_dir
    except (OSError, KeyError):
        return str(Path.home())


def _expand(path: str, home: str) -> str:
    """Resolve a Flatpak ``~`` or ``xdg-data`` entry against ``home``."""
    if path.startswith("~"):
        return home + path[1:]
    if path == "xdg-data" or path.startswith("xdg-data/"):
        return f"{home}/.local/share" + path[len("xdg-data"):]
    return path


def profile_writable(filesystems: list[str], profile: Path, home: str) -> bool:
    """True when a Flatpak granted ``filesystems`` can write ``profile``."""
    target = str(profile) + "/"
    for entry in filesystems:
        path, _, mode = entry.partition(":")
        if path.startswith("!") or mode == "ro":
            continue
        if path in ("home", "host"):
            return True
        grant = _expand(path, home).rstrip("/") + "/"
        if grant.startswith("/") and target.startswith(grant):
            return True
    return False


def _browser_block(owner: Path, profile: Path) -> dict[str, Any]:
    """Which Edge is installed, and whether it can save to the profile."""
    scope = _flatpak_scope(owner)
    if scope:
        _, perms = _run(owner, ["flatpak", "info", f"--{scope}", "--show-permissions", _FLATPAK_APP])
        fs = _filesystems(perms)
        return {
            "kind": "flatpak", "scope": scope, "filesystems": fs,
            "profile_writable": profile_writable(fs, profile, _owner_home(owner)),
        }
    native = next((found for name in _NATIVE_BINS if (found := shutil.which(name))), None)
    if native:
        return {"kind": "native", "path": native, "profile_writable": None}
    return {"kind": "absent"}


def _log_block(log: Path) -> dict[str, Any]:
    """How often Edge aborted, and the last abort line."""
    try:
        with log.open("rb") as fh:
            with contextlib.suppress(OSError):
                fh.seek(-_LOG_TAIL_BYTES, 2)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {"exists": False}
    fatal = [line for line in tail.splitlines() if ":FATAL:" in line]
    return {
        "exists": True,
        "fatal_count": len(fatal),
        "last_fatal": fatal[-1][:_MAX_LINE_CHARS] if fatal else "",
    }
