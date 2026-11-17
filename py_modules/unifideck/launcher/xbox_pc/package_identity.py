"""launcher/xbox_pc/package_identity.py — the Windows package names of a game.

Windows gives a Store app a package identity; Wine gives none, so
``GetPackageFamilyName`` reports no package. A Store build can branch on that
and run as another edition: one title then loads its Steam online module and
quits when Steam refuses it (ISSUES.md I-085). The runtime reports the names
this module derives from ``MicrosoftGame.config`` when the game's env carries
them (``title_env``).

The family name is ``<Name>_<PublisherId>``; the full name is
``<Name>_<Version>_<Architecture>__<PublisherId>`` (no resource id). The
publisher id is the first 64 bits of SHA-256 over the UTF-16LE publisher,
written as 13 Crockford base32 characters (the runtime's xgameruntime does the
same for its own use).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_NAME_RE = re.compile(r"[A-Za-z0-9.\-]{3,50}")
_VERSION_RE = re.compile(r"\d{1,5}(?:\.\d{1,5}){3}")
_ARCHITECTURES = frozenset({"x64", "x86", "arm", "arm64", "neutral"})


@dataclass(frozen=True)
class PackageNames:
    """A package's family and full name."""

    family: str
    full: str


def publisher_id(publisher: str) -> str:
    """The 13-character publisher id Windows derives from ``publisher``."""
    digest = hashlib.sha256(publisher.encode("utf-16-le")).digest()
    bits = int.from_bytes(digest[:8], "big") << 1  # 65 bits: 13 groups of 5
    return "".join(_ALPHABET[(bits >> (60 - 5 * i)) & 31] for i in range(13))


def package_names(
    name: str | None, publisher: str | None, version: str | None, architecture: str | None,
) -> PackageNames | None:
    """The names of the package, or None when the identity is unusable.

    A missing or unknown architecture is ``x64``, the GDK's PC default.
    """
    if not name or not publisher or not _NAME_RE.fullmatch(name):
        return None
    if not version or not _VERSION_RE.fullmatch(version):
        return None
    arch = (architecture or "x64").lower()
    if arch not in _ARCHITECTURES:
        arch = "x64"
    pid = publisher_id(publisher)
    return PackageNames(family=f"{name}_{pid}", full=f"{name}_{version}_{arch}__{pid}")
