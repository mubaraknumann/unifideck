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
3. **the plugin's browser**: which Chromium builds are downloaded, which one was
   verified, and whether its ``chrome`` is executable. It replaced the Edge
   flatpak in 0.8.0 (``auth/edge_browser/chrome_installer.py``);
4. **browser aborts**: ``FATAL`` lines in ``edge-auth.log``. An abort skips
   the shutdown that writes recent cookies to disk.

Since 0.8.0 a stream plays in Unifideck's own player, which does not use these
cookies; they matter only when a stream falls back to xbox.com.

Everything is best-effort and read-only. The database is read from a copy so
a running Edge never sees a lock.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Same layout as ``auth/edge_browser/chrome_installer.py``. Not imported: a
#: probe must not pull the auth layer into the bundle.
_BROWSER_DIR = "browser"
_MARKER = ".verified"

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

#: Chromium stores times as microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.UTC)


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
        "browser": browser_block(Path(data_dir)),
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
        moment = _CHROME_EPOCH + dt.timedelta(microseconds=int(value))
    except (OverflowError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def _file_facts(path: Path) -> dict[str, Any]:
    """Present, size and last-written time of one file."""
    try:
        st = path.stat()
    except OSError:
        return {"exists": False}
    written = dt.datetime.fromtimestamp(st.st_mtime, tz=dt.UTC)
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


def browser_block(data_dir: Path) -> dict[str, Any]:
    """The plugin's browser: verified version and whether it can run."""
    root = data_dir / _BROWSER_DIR
    # ``chromium-*`` (ungoogled-chromium) and the ``chrome-*`` build that 0.7.7 dev builds
    # shipped first. The binary is found, not assumed: each build nests it in
    # its own archive folder.
    builds = sorted(p.name for p in root.iterdir()
                    if p.is_dir() and p.name.startswith(("chromium-", "chrome-"))) \
        if root.is_dir() else []
    for name in reversed(builds):
        marker = root / name / _MARKER
        if marker.is_file():
            binary = next((p for p in (root / name).glob("*/chrome") if p.is_file()),
                          root / name / "chrome")
            return {"kind": "bundled", "version": marker.read_text().strip(),
                    "path": str(binary), "executable": os.access(binary, os.X_OK),
                    "builds": builds}
    return {"kind": "absent", "builds": builds}


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
