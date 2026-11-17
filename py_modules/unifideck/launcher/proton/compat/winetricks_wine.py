"""compat/winetricks_wine.py — redistributables under a Proton that ships no winetricks.

umu's winetricks verb runs ``<PROTONPATH>/protonfixes/winetricks``, which only
GE-Proton and UMU-Proton ship. Official Valve Protons (Experimental, Proton
9/10, ...) have none, yet a user may choose one for any game, and the game
still needs its VC++ runtimes. Borrowing GE for the step is not an option: a
prefix set up by two Protons keeps symlinks into both, and one Proton's Wine
then writes through them into the other's shared install.

So this runs the plugin's bundled ``winetricks`` with the game's own Proton's
Wine, which is all GE's own hook does (it sets ``WINE``/``WINESERVER`` to its
binaries and runs winetricks). Measured on a Steam Deck, 2026-11-17, on a
prefix Proton Experimental created: all nine default packages installed as
real Microsoft DLLs in ~65 s, a following Experimental launch kept them with no
prefix upgrade, and Experimental's files were untouched. Two things it needs:

* ``cabextract``, which SteamOS does not ship and three default packages
  (vcrun2022, mfc140, d3dcompiler_43) need to unpack Microsoft's cab files.
  The plugin bundles it in ``bin/cabextract/`` (see build-plugin.sh); only
  that folder goes on winetricks' ``PATH``.
* real files, not symlinks, in the prefix's ``winsxs``. A Valve Proton links
  the VC80/VC90 side-by-side DLLs there into its own read-only install, and
  the VC++ 2008 installer then fails with access denied (``0x80070005``)
  writing over them. Copies, made in the prefix being set up, install fine.

Stdlib-only at import: runs in the out-of-process launcher.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.setup_env import build_setup_env

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

_LIB_DIRS = ("files/lib/wine", "files/lib64/wine")


def winsxs_links_to_copies(prefix_root: Path, proton_dir: Path) -> int:
    """Replace the prefix's ``winsxs`` symlinks into ``proton_dir`` with copies.

    Only links that point inside ``proton_dir`` are touched, and each becomes
    a writable copy of the same file, so the prefix holds what it held before
    and an installer can now overwrite it. Returns how many were copied.
    Never raises; an entry it cannot copy is left as it was.
    """
    winsxs = prefix_root / "drive_c" / "windows" / "winsxs"
    if not winsxs.is_dir():
        return 0
    base = str(proton_dir.resolve()) + os.sep
    copied = 0
    for dirpath, _dirs, files in os.walk(winsxs):
        for name in files:
            copied += _copy_over_link(Path(dirpath) / name, base)
    return copied


def _copy_over_link(link: Path, proton_base: str) -> bool:
    """Turn one symlink into ``proton_base`` into a writable copy; True if done."""
    try:
        if not link.is_symlink():
            return False
        target = link.resolve()
        if not str(target).startswith(proton_base) or not target.is_file():
            return False
        tmp = link.with_name(link.name + ".unifideck-copy")
        shutil.copyfile(target, tmp)
        tmp.chmod(0o755)
        tmp.replace(link)
    except OSError as e:
        logger.debug("[compat.winetricks] could not copy %s: %s", link, e)
        return False
    return True


def find_cabextract(plugin_dir: Path) -> Path | None:
    """A ``cabextract`` that actually runs, or None.

    The plugin's bundled ``bin/cabextract/cabextract`` first, then the
    host's. Each is asked ``--version`` rather than trusted for existing: a
    truncated binary passes an exists-and-executable check but crashes
    (measured: rc 139), and the packager may not keep the exec bit
    (CLAUDE.md rule 7).
    """
    candidates = [plugin_dir / "bin" / "cabextract" / "cabextract"]
    host = shutil.which("cabextract")
    if host:
        candidates.append(Path(host))
    for tool in candidates:
        if tool.is_file() and not os.access(tool, os.X_OK):
            with contextlib.suppress(OSError):
                tool.chmod(tool.stat().st_mode | 0o111)
    return next((tool for tool in candidates if _runs(tool)), None)


def _runs(tool: Path) -> bool:
    """Whether ``tool --version`` exits 0 within a few seconds."""
    try:
        return subprocess.run(
            [str(tool), "--version"], capture_output=True, timeout=5, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def command(
    plan: ProtonLaunchPlan,
    packages: list[str],
    proton_dir: Path,
    prefix_root: Path,
    cabextract: Path,
) -> tuple[list[str], dict[str, str]] | None:
    """``(argv, env)`` running bundled winetricks with ``proton_dir``'s Wine.

    ``cabextract`` (from :func:`find_cabextract`) goes last on ``PATH``, so a
    host tool of the same name still wins.

    None when the Proton has no ``files/bin/wine`` or the plugin's
    ``bin/winetricks`` is missing; the caller records the step as failed.
    """
    wine = proton_dir / "files" / "bin" / "wine"
    winetricks = plan.context.plugin_dir / "bin" / "winetricks"
    if not wine.is_file() or not winetricks.is_file():
        logger.error(
            "[compat.winetricks] cannot install redistributables: wine=%s (%s), winetricks=%s (%s)",
            wine, wine.is_file(), winetricks, winetricks.is_file(),
        )
        return None
    env = build_setup_env(plan)
    path = [str(proton_dir / "files" / "bin"), env.get("PATH") or os.environ.get("PATH", "/usr/bin:/bin")]
    path.append(str(cabextract.parent))
    env.update({
        "WINEPREFIX": str(prefix_root),
        "WINE": str(wine),
        "WINELOADER": str(wine),
        "WINESERVER": str(proton_dir / "files" / "bin" / "wineserver"),
        "WINEDLLPATH": os.pathsep.join(str(proton_dir / d) for d in _LIB_DIRS if (proton_dir / d).is_dir()),
        "PATH": os.pathsep.join(path),
        "WINETRICKS_LATEST_VERSION_CHECK": "disabled",
    })
    # sh, not the file's exec bit: the packager may not keep it (CLAUDE.md rule 7).
    return ["/bin/sh", str(winetricks), "-q", *packages], env
