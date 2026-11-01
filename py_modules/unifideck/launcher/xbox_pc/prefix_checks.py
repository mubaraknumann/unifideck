"""launcher/xbox_pc/prefix_checks.py — runtimes a game needs in or beside its prefix.

Two checks, both read-only:

* **VC++.** The version the prefix registers. A game that declares VC++
  needs 14.44.35211.0 or newer (Nexus ``game-dependencies.ts``);
  :mod:`.prefix_vcredist` installs it from Steam's or Microsoft's
  redistributable, since a game's own copy may be encrypted.
* **EasyAntiCheat and BattlEye.** A game with either needs Steam's own
  "Proton EasyAntiCheat Runtime" or "Proton BattlEye Runtime". It is used
  when installed and never downloaded here. The runtime's ``ntdll`` reads
  ``PROTON_EAC_RUNTIME`` and ``PROTON_BATTLEYE_RUNTIME`` itself, so the
  runner only has to export them.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

VC_REDIST_VERSION = "14.44.35211.0"
EAC_RUNTIME_DIR = "steamapps/common/Proton EasyAntiCheat Runtime"
_EAC_FILES = ("easyanticheat_x64.dll", "easyanticheat_x64.so")
BATTLEYE_RUNTIME_DIR = "steamapps/common/Proton BattlEye Runtime"
#: Where the runtime's ntdll looks under ``PROTON_BATTLEYE_RUNTIME``.
_BATTLEYE_LIB_DIR = "v1/lib64/wine"

#: Steam app ids of the two runtimes, for ``steam://install/<id>``.
EAC_RUNTIME_APPID = 1826330
BATTLEYE_RUNTIME_APPID = 1161040

#: ``arch -> its VC++ 14 key in system.reg``. The x86 redistributable
#: registers under ``Wow6432Node`` in a 64-bit prefix.
_VC_SECTION_RES = {
    "x64": re.compile(
        r"^\[Software\\\\Microsoft\\\\VisualStudio\\\\14\.0\\\\VC\\\\Runtimes\\\\x64\](?:\s|$)", re.M,
    ),
    "x86": re.compile(
        r"^\[Software\\\\Wow6432Node\\\\Microsoft\\\\VisualStudio\\\\14\.0\\\\VC\\\\Runtimes\\\\x86\](?:\s|$)",
        re.M,
    ),
}
_INSTALLED_RE = re.compile(r'^"Installed"=dword:00000001$', re.M)
_VERSION_RE = re.compile(r'^"Version"="v?([0-9.]+)"$', re.M | re.I)


def installed_vc_runtime(prefix: Path, arch: str = "x64") -> str | None:
    """The VC++ 14 version for ``arch`` (x64 or x86) in ``prefix``'s ``system.reg``, or None."""
    try:
        registry = (prefix / "system.reg").read_text(errors="replace")
    except OSError:
        return None
    match = _VC_SECTION_RES[arch].search(registry)
    if not match:
        return None
    end = registry.find("\n[", match.start() + 1)
    section = registry[match.start(): end if end >= 0 else None]
    if not _INSTALLED_RE.search(section):
        return None
    version = _VERSION_RE.search(section)
    return version.group(1) if version else None


def version_at_least(actual: str, minimum: str) -> bool:
    """Whether dotted version ``actual`` is ``minimum`` or newer."""
    try:
        a = [int(p) for p in actual.split(".")]
        b = [int(p) for p in minimum.split(".")]
    except ValueError:
        return False
    width = max(len(a), len(b))
    return (a + [0] * (width - len(a))) >= (b + [0] * (width - len(b)))


def installed_eac_runtime(libraries: Iterable[Path]) -> Path | None:
    """Steam's Proton EasyAntiCheat Runtime in any library, or None.

    ``libraries`` are the Steam library roots
    (``utils.vdf_compat.steam_library_dirs``, resolved by the caller).
    """
    for library in sorted(set(libraries)):
        root = library / EAC_RUNTIME_DIR
        if all((root / "v2" / "lib64" / name).is_file() for name in _EAC_FILES):
            return root
    return None


def installed_battleye_runtime(libraries: Iterable[Path]) -> Path | None:
    """Steam's Proton BattlEye Runtime in any library, or None."""
    for library in sorted(set(libraries)):
        root = library / BATTLEYE_RUNTIME_DIR
        if (root / _BATTLEYE_LIB_DIR).is_dir():
            return root
    return None
