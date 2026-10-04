"""Choose which of a game's uploads to install.

An itch.io game can carry many uploads: per-OS builds, demos, soundtracks,
an HTML5 version, links to other hosts. We pick one ourselves instead of
asking butler, because on SteamOS **butler hides every Windows upload**:
it only counts Windows as a supported host when ``wine`` is on ``PATH``, so
``Install.GetUploads`` returned ``[]`` for every Windows-only game (measured,
butler 15.31.0). ``Fetch.GameUploads`` with ``compatible: false`` lists them
all, and this module ranks them:

1. only ``type == "default"`` (a build of the game; not ``html``,
   ``soundtrack``, ``book`` …) hosted on itch.io (``storage != "external"``),
   in a format butler can install (not ``.deb``/``.rpm``/``.dmg``/``.pkg``/
   ``.msi``, since butler 15.31 no longer runs installers);
2. Linux before Windows (the user's standing preference; Proton is the
   fallback, not the default);
3. a full build before a demo (the smallest Windows upload on the test
   account was ``CroakingAroundDEMO.zip``);
4. an archive before a bare file, and 64-bit before 32-bit.

**Untagged uploads.** Many developers never tick the platform boxes: both of
DELTARUNE's uploads have ``platforms: {}``, and only the filenames say
``(PC Version 3).zip`` / ``(Mac Version 3).zip``. Such an upload gets its
platform from the filename (:func:`upload_platforms`) and ranks after every
tagged one. A wrong guess cannot ship a broken shortcut: the launch target is
chosen from what the archive actually contains (``exe.py``).
"""
from __future__ import annotations

import re
from typing import Any

_INSTALLABLE_TYPE = "default"
_UNSUPPORTED_SUFFIXES = (".deb", ".rpm", ".dmg", ".pkg", ".msi", ".apk")
_ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")
_THIRTY_TWO_BIT = re.compile(r"(?<![a-z0-9])(32|x86|i[36]86|win32|linux32)(?![a-z0-9_])", re.I)


_FILENAME_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("osx", re.compile(r"(?<![a-z])(mac|macos|osx|darwin)(?![a-z])|\.(dmg|app)(\.zip)?$", re.I)),
    ("linux", re.compile(r"(?<![a-z])(linux|ubuntu|debian|steamos)(?![a-z])"
                         r"|\.(x86_64|appimage|sh)$", re.I)),
    ("windows", re.compile(r"(?<![a-z])(pc|win|windows|win32|win64)(?![a-z])|\.exe$", re.I)),
)


def upload_platforms(upload: dict[str, Any]) -> dict[str, Any]:
    """The upload's platform tags, or a filename guess when it has none.

    The guess is exclusive: a filename that names a Mac build is never read
    as Windows because it also says "PC".
    """
    tagged = upload.get("platforms") or {}
    if tagged:
        return tagged
    name = str(upload.get("filename") or upload.get("displayName") or "")
    for platform, pattern in _FILENAME_HINTS:
        if pattern.search(name):
            return {platform: "inferred"}
    return {}


def is_installable(upload: dict[str, Any]) -> bool:
    """A hosted game build for Linux or Windows that butler can unpack."""
    if upload.get("type") != _INSTALLABLE_TYPE or upload.get("storage") == "external":
        return False
    name = str(upload.get("filename") or "").lower()
    if name.endswith(_UNSUPPORTED_SUFFIXES):
        return False
    platforms = upload_platforms(upload)
    return bool(platforms.get("linux") or platforms.get("windows"))


def _rank(upload: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Lower sorts first."""
    name = str(upload.get("filename") or upload.get("displayName") or "")
    return (
        0 if upload.get("platforms") else 1,
        0 if upload_platforms(upload).get("linux") else 1,
        1 if upload.get("demo") else 0,
        0 if name.lower().endswith(_ARCHIVE_SUFFIXES) else 1,
        1 if _THIRTY_TWO_BIT.search(name) else 0,
    )


def has_web_build(uploads: list[dict[str, Any]]) -> bool:
    """An HTML5 build hosted on itch.io: playable in a browser, not installable."""
    return any(u.get("type") == "html" and u.get("storage") != "external" for u in uploads)


def choose_upload(uploads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The upload to install, or None when the game has nothing we can run."""
    candidates = [u for u in uploads if is_installable(u)]
    if not candidates:
        return None
    return min(candidates, key=_rank)
