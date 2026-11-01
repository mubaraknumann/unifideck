"""launcher/xbox_pc/prefix_vcredist.py — Microsoft's VC++ runtime in a game's prefix.

A GDK game declares VC++ 14 as ``KnownDependency VC14`` in its config or as
the ``Microsoft.VCLibs.140.00`` framework package in its appx manifest. On
Windows that framework puts Microsoft's ``concrt140.dll``, ``msvcp140*.dll``
and ``vcruntime140*.dll`` on the game's search path. Here nothing does, so
the game loads Wine's builtins, which leave exports unimplemented (I-047:
``concrt140``'s ``_Concurrent_queue_iterator_base_v4`` aborted a game after
minutes of play).

So the redistributable is installed into the prefix once, with the
runtime's own ``proton``, and the runner loads those DLLs native first.
There is one per architecture: x64 fills ``system32``, x86 fills
``syswow64`` (a 32-bit game reads only that). Each installer comes from
Steam's Common Redistributables when a library has it, else from one
download kept in the Xbox PC data folder. Each candidate is asked what it
is (a PE image of a plausible size), not trusted by name.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import logging
import os
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path

from .layout import data_root
from .prefix import run_proton
from .prefix_checks import VC_REDIST_VERSION, installed_vc_runtime, version_at_least
from .runtime import Runtime

logger = logging.getLogger(__name__)

#: ``arch -> the prefix folder its DLLs go to``.
VC_ARCHES = {"x64": "system32", "x86": "syswow64"}
STEAM_REDIST_DIR = "steamapps/common/Steamworks Shared/_CommonRedist/vcredist/2022"
INSTALL_TIMEOUT_S = 600.0
_MIN_INSTALLER_BYTES = 1 << 20
_BUILTIN_MARKER = b"Wine builtin DLL"


def installer_name(arch: str) -> str:
    """``VC_redist.x64.exe`` or ``VC_redist.x86.exe``."""
    return f"VC_redist.{arch}.exe"


def download_url(arch: str) -> str:
    """Microsoft's link to the latest VC++ 2015-2022 redistributable for ``arch``."""
    return f"https://aka.ms/vs/17/release/vc_redist.{arch}.exe"


def _is_installer(path: Path) -> bool:
    """Whether ``path`` is a readable PE image of an installer's size."""
    try:
        if path.stat().st_size < _MIN_INSTALLER_BYTES:
            return False
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


def steam_vc_redist(libraries: Iterable[Path], arch: str = "x64") -> Path | None:
    """Steam's VC++ 2022 installer for ``arch`` in any library, or None."""
    for library in sorted(set(libraries)):
        candidate = library / STEAM_REDIST_DIR / installer_name(arch)
        if _is_installer(candidate):
            return candidate
    return None


def download_vc_redist(arch: str = "x64", url: str | None = None, timeout_s: float = 120.0) -> Path:
    """The installer for ``arch`` from Microsoft, kept once under the data folder. Raises ``OSError``.

    The launcher runs under the system Python, so the default context has
    the host's certificate store and the download is verified.
    """
    url = url or download_url(arch)
    folder = data_root() / "vcredist"
    target = folder / installer_name(arch)
    if _is_installer(target):
        return target
    folder.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.part-{os.getpid()}")
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response, temporary.open("wb") as fh:
            while chunk := response.read(1 << 16):
                fh.write(chunk)
        if not _is_installer(temporary):
            raise OSError(f"{url} did not return an installer")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def native_vc_present(prefix: Path, arch: str = "x64") -> bool:
    """Whether ``arch``'s ``concrt140.dll`` is Microsoft's, not Wine's builtin."""
    try:
        with (prefix / "drive_c/windows" / VC_ARCHES[arch] / "concrt140.dll").open("rb") as fh:
            head = fh.read(1 << 16)
    except OSError:
        return False
    return head[:2] == b"MZ" and _BUILTIN_MARKER not in head


def vc_runtime_ready(prefix: Path, arch: str = "x64") -> bool:
    """Whether the prefix registers ``arch``'s VC++ new enough and holds Microsoft's DLLs."""
    installed = installed_vc_runtime(prefix, arch)
    return bool(installed and version_at_least(installed, VC_REDIST_VERSION)) and native_vc_present(prefix, arch)


async def install_vc_runtime(
    runtime: Runtime, compat_root: Path, env: Mapping[str, str], installer: Path,
    arch: str = "x64", timeout_s: float = INSTALL_TIMEOUT_S,
) -> str | None:
    """Run ``arch``'s ``installer`` quietly in the prefix. Returns why it failed, or None.

    The result is judged by what the prefix holds afterwards, not by the
    exit code: Wine hands Linux only the low byte of a Windows exit code,
    so "a newer version is already installed" (1638) arrives as 102.
    """
    logger.info("[xbox_pc.vcredist] installing %s into %s", installer, compat_root)
    rc, tail = await run_proton(runtime, env, timeout_s, str(installer), "/install", "/quiet", "/norestart")
    installed = installed_vc_runtime(compat_root, arch)
    if not installed or not version_at_least(installed, VC_REDIST_VERSION):
        return (f"{installer} exited {rc} and left {arch} VC++ {installed or 'unregistered'}, "
                f"need {VC_REDIST_VERSION}: {tail[-300:]}")
    if not native_vc_present(compat_root, arch):
        return f"{installer} exited {rc} and left Wine's concrt140.dll in {VC_ARCHES[arch]}"
    return None
