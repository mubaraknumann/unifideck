"""auth.edge_browser.chrome_installer — the plugin's own browser (no flatpak).

Every browser window the plugin opens (store sign-ins, storefronts, itch.io
web games, xCloud) runs in Google's standalone "Chrome for Testing" build,
downloaded once into ``~/.local/share/unifideck/browser/`` like GE-Proton.
It replaced the Microsoft Edge flatpak in 0.7.7.

Why this build: xCloud streams H.264, which open-source Chromium builds do not
decode, and the sign-in flows need a browser the plugin can drive over CDP.
Measured on a Steam Deck 2026-11-04: it runs with SteamOS's own libraries (no
missing ``ldd`` entries), and streams 1080p60 with audio and controller input.

The download is pinned: ``CHROME_VERSION`` + ``CHROME_SHA256``. Bumping it is a
release step (``unifideck-release`` skill). A file is trusted only once it is
the pinned zip, unpacked, executable and answering ``--version``.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import stat
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from unifideck.core.net import ssl_ctx_strict

logger = logging.getLogger(__name__)

CHROME_VERSION = "156.0.8078.80"
CHROME_URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    f"{CHROME_VERSION}/linux64/chrome-linux64.zip"
)
CHROME_SHA256 = "b711c720f774be355197f32edf37e0651418ba036d92446ee2da19ac017540a7"
#: Shown to the user before the download (MB).
CHROME_DOWNLOAD_MB = 200
#: ``install_blocking``'s error when the download itself failed (no network).
NETWORK_FAILURE = "microsoft.chromiumInstallTimeout"
_MARKER = ".verified"
_CHUNK = 1 << 20


def browser_root() -> Path:
    """Where the plugin's browser lives (resolved at call time)."""
    return Path("~/.local/share/unifideck/browser").expanduser()


def install_dir(version: str = CHROME_VERSION) -> Path:
    """The unpacked build for ``version``."""
    return browser_root() / f"chrome-{version}"


def chrome_binary(version: str = CHROME_VERSION) -> Path:
    """The ``chrome`` executable inside the unpacked build."""
    return install_dir(version) / "chrome-linux64" / "chrome"


def is_ready(version: str = CHROME_VERSION) -> bool:
    """True when the pinned build was verified and is still executable."""
    binary = chrome_binary(version)
    return (install_dir(version) / _MARKER).is_file() and os.access(binary, os.X_OK)


def _download(dest: Path) -> str:
    """Stream the pinned zip to ``dest``; return its SHA-256. Raises ``OSError``."""
    digest = hashlib.sha256()
    req = urllib.request.Request(CHROME_URL, headers={"User-Agent": "Unifideck"})
    with urllib.request.urlopen(req, timeout=60, context=ssl_ctx_strict()) as r, \
            dest.open("wb") as out:
        while chunk := r.read(_CHUNK):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest``, keeping the stored Unix permissions.

    ``zipfile`` drops them, and a ``chrome`` without its executable bit is
    exactly the "present but broken" state CLAUDE.md rule 7 warns about.
    """
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = dest / info.filename
            if not target.resolve().is_relative_to(dest.resolve()):
                raise OSError(f"unsafe path in browser archive: {info.filename}")
            z.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                target.chmod(mode | stat.S_IRUSR)


def _answers_version(binary: Path) -> bool:
    try:
        done = subprocess.run([str(binary), "--version"], capture_output=True, timeout=30,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and CHROME_VERSION.encode() in done.stdout


def _remove_other_versions() -> None:
    for old in browser_root().glob("chrome-*"):
        if old.is_dir() and old != install_dir():
            shutil.rmtree(old, ignore_errors=True)


def _fetch_and_verify(archive: Path, staging: Path) -> str:
    """Download, check and unpack into ``staging``; ``""`` or an error i18n key.

    Raises ``OSError`` when the download itself fails.
    """
    logger.info("[Browser] downloading Chrome %s (~%d MB)", CHROME_VERSION, CHROME_DOWNLOAD_MB)
    digest = _download(archive)
    if digest != CHROME_SHA256:
        logger.warning("[Browser] downloaded browser has the wrong checksum (%s); "
                       "not installed", digest[:16])
        return "microsoft.browserInstallFailed"
    shutil.rmtree(staging, ignore_errors=True)
    _unpack(archive, staging)
    if not _answers_version(staging / "chrome-linux64" / "chrome"):
        logger.warning("[Browser] the downloaded browser does not run on this system")
        return "microsoft.browserInstallFailed"
    return ""


def _activate(staging: Path) -> None:
    """Move a verified build into place and mark it ready."""
    shutil.rmtree(install_dir(), ignore_errors=True)
    staging.rename(install_dir())
    (install_dir() / _MARKER).write_text(CHROME_VERSION)
    _remove_other_versions()
    logger.info("[Browser] Chrome %s installed at %s", CHROME_VERSION, install_dir())


def install_blocking() -> dict[str, Any]:
    """Download, verify and unpack the pinned build. Never raises.

    Returns ``{success, message}`` or ``{success, error}`` with i18n keys, the
    contract the frontend's install modal already reads.
    """
    if is_ready():
        return {"success": True, "message": "microsoft.browserAlreadyInstalled"}
    root = browser_root()
    archive = root / f".chrome-{CHROME_VERSION}.zip.part"
    staging = root / f".chrome-{CHROME_VERSION}.unpacking"
    try:
        root.mkdir(parents=True, exist_ok=True)
        error = _fetch_and_verify(archive, staging)
        if error:
            return {"success": False, "error": error}
        _activate(staging)
        return {"success": True, "message": "microsoft.browserInstalled"}
    except OSError as e:
        logger.warning("[Browser] browser download failed: %s", e)
        return {"success": False, "error": NETWORK_FAILURE}
    finally:
        with contextlib.suppress(OSError):
            archive.unlink()
        shutil.rmtree(staging, ignore_errors=True)


class ChromeInstaller:
    """The installer ``EdgeBrowser`` composes: find, check, install."""

    def __init__(self) -> None:
        # One download at a time: the startup download and the install
        # modal can both ask, and they share the staging paths.
        self._lock = asyncio.Lock()

    def find_cmd(self) -> list[str] | None:
        """The browser command, or None until it is installed."""
        return [str(chrome_binary())] if is_ready() else None

    @property
    def is_installed(self) -> bool:
        """True once the pinned build is verified."""
        return is_ready()

    async def install(self) -> dict[str, Any]:
        """Download and verify the browser off the event loop, one at a time."""
        async with self._lock:
            return await asyncio.to_thread(install_blocking)
