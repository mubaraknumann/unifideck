"""Pick the launch target out of an installed game folder — native or Windows.

Shared by the stores that hand us a folder of files and no launch manifest:
GameVault (archives the owner uploaded) and itch.io (whatever the developer
zipped). It began in ``stores/gamevault/exe_finder.py`` and was promoted here
when itch.io became the second consumer, rather than grown a second scorer
beside it (``validate_architecture`` ``SHARED_HELPERS`` pins the one copy).
Not to be confused with ``core/exe_finder.py``, the ``.exe``-only resolver the
CLI stores use when their manifest names no launch target.

Native Linux builds are first-class. A folder of DRM-free games on a Steam
Deck is routinely half native, and an ``.exe``-only scorer returned nothing for
those: the marker got ``exe_path: ""`` and reconcile wrote a games.map row with
no target, i.e. an install that reports success and can never launch.

The keyword filter is a *preference*, not a validity test, so it degrades
instead of eliminating: when it rejects everything, the best rejected
candidate is returned rather than nothing. That distinction was worth a
release. A GameVault archive is whatever its owner uploaded, and a repack's
only executable is its installer — the first real install produced exactly
one ``.exe``,

    Ghost of Tsushima [DODI Repack]/Setup.exe

which :data:`_UTIL_KEYWORDS` rejects on "setup". With no fallback the shortcut
had no target at all. Handing back the installer at least gives the user
something to run.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# Substrings that mark an executable as a bundled utility rather than the
# game. ``unins`` rather than ``uninstall``: Inno Setup, which most of these
# archives are built with, names its uninstaller ``unins000.exe`` — a name
# that contains none of the longer words and was scoring as the game
# whenever the filesystem happened to walk it first.
_UTIL_KEYWORDS = (
    "unins", "uninstall", "setup", "install", "redist", "vcredist",
    "directx", "dxsetup", "ue4", "ue5", "crash", "report",
    "_commonredist", "support", "dotnet",
    # Chromium runtimes (NW.js, Electron, CEF) ship helper ELFs beside the
    # real binary, and they are bigger than it: itch.io's NW.js builds carry
    # ``nacl_helper`` (2.7 MB) next to ``nw`` (222 KB), so the size score
    # picked the helper. Measured on a real install, 2026-09-23.
    "helper", "sandbox", "crashpad",
)

# Directories that never hold the launch target and can be large. Pruning
# them keeps the walk proportional to the game's own tree rather than to
# every shipped shared library.
_PRUNE_DIRS = frozenset(
    {
        "_commonredist", "directx", "redist", "vcredist", "dotnet",
        "lib", "lib64", "libs", "share", "locale", "resources",
        "__macosx", ".git",
    }
)

_GOOD_DEPTH = 3   # prefer shallow paths
_SIZE_CAP_MB = 100
_MAX_SIZE_POINTS = 5.0

# Native launcher names that are conventionally the entry point, best first.
_NATIVE_ENTRY_NAMES = ("start.sh", "run.sh", "play.sh", "launch.sh")

_KIND_WINDOWS = "windows"
_KIND_NATIVE = "native"

_ELF_MAGIC = b"\x7fELF"
_SHEBANG = b"#!"

# Files that mark a directory as a MonoKickstart *Linux* build. Such a build
# ships a PE32 ``.exe`` — a Mono assembly, not a Windows program — beside a
# shell script and native ``.so``s, and only the script can start it. GOG's
# Linux Bastion is the case that exposed this: ``game/Bastion.exe`` is
# ``PE32 ... Intel i386 Mono/.Net assembly``, and handing it to Proton gets a
# game that never launches. The launcher already routes anything that is not
# ``.exe``/``.cmd``/``.bat`` down its native path (``LaunchContext``'s
# ``is_native_linux``), so picking the script is the entire fix.
_MONO_KICKSTART_MARKERS = ("monoconfig", "monomachineconfig")
_MONO_LIB_GLOB = "libmono-*.so*"


def find_executable(
    install_dir: str,
    *,
    prefer_native: bool = False,
    title: str | None = None,
) -> str | None:
    """Best-guess launch target under *install_dir*, or None if there is none.

    *prefer_native* comes from the store: GameVault's archive type token
    (``L_P``/``L_SW``), itch.io's chosen upload platform. It only reorders the
    two pools — a mislabelled archive still resolves, because whichever pool
    is empty is skipped rather than treated as an answer.

    *title* is the game's name, used only to recognise a launcher named after
    the game (GOG's Linux builds do this instead of shipping ``start.sh``).
    Optional so the remote path, which has no title to offer here, is
    unaffected.
    """
    pools = _collect(install_dir, title=title)
    order = (
        (_KIND_NATIVE, _KIND_WINDOWS)
        if prefer_native
        else (_KIND_WINDOWS, _KIND_NATIVE)
    )

    for kind in order:
        preferred = pools[kind]["preferred"]
        if preferred:
            return max(preferred)[1]

    for kind in order:
        rejected = pools[kind]["rejected"]
        if rejected:
            best = max(rejected)[1]
            logger.warning(
                "[launch target] nothing under %s looks like the game itself; "
                "using %s. If this archive is an installer, the game still has "
                "to be installed from it before it will launch — and if it is "
                "a native build, its launcher script may be missing.",
                install_dir, Path(best).name,
            )
            return best
    return None


def is_named_native_entry(path: str, title: str | None = None) -> bool:
    """True for a native launcher named by convention or after the game.

    The strong native signal: ``start.sh``-style names, or a script/binary
    named after the title (GOG's ``game/Bastion``, a Ren'Py ``Game.sh``).
    itch.io uses it to let a cross-platform build inside a *Windows* upload —
    Ren'Py PC zips ship ``lib/linux-x86_64`` and ``<Game>.sh`` — run natively,
    without letting any stray script in a Windows zip win.
    """
    name = os.path.basename(path).lower()
    return name in _NATIVE_ENTRY_NAMES or _matches_title(name, title)


def _collect(
    install_dir: str, *, title: str | None = None,
) -> dict[str, dict[str, list[tuple[float, str]]]]:
    """``{kind: {"preferred": [...], "rejected": [...]}}`` for the whole tree.

    Separate from the pick so :func:`find_executable` stays under the
    cognitive-complexity gate.
    """
    pools: dict[str, dict[str, list[tuple[float, str]]]] = {
        _KIND_WINDOWS: {"preferred": [], "rejected": []},
        _KIND_NATIVE: {"preferred": [], "rejected": []},
    }
    for kind, full, depth_score, demote in _iter_candidates(install_dir):
        scored = (depth_score + _bonus(kind, full, title), full)
        rejected = demote or _looks_like_a_utility(full)
        if demote:
            logger.info(
                "[launch target] %s is a Mono assembly in a native Linux "
                "build; keeping it only as a fallback", full,
            )
        pools[kind]["rejected" if rejected else "preferred"].append(scored)
    return pools


def _iter_candidates(
    install_dir: str,
) -> Iterator[tuple[str, str, float, bool]]:
    """``(kind, path, depth_score, demote)`` for every plausible launch target.

    *demote* marks a candidate that is launchable in principle but is the wrong
    target here — currently only a Mono assembly in a native Linux build. It
    goes to the same fallback pool as a utility ``.exe``, so a real native
    entry point wins and an archive with nothing else still resolves.

    The Mono-Linux-build probe runs once per directory, not once per file: the
    markers are siblings of the ``.exe``, so a whole game tree costs one extra
    ``iterdir``-worth of checks rather than one per candidate.
    """
    root = Path(install_dir)
    for dirpath, dirs, files in os.walk(install_dir):
        dirs[:] = [d for d in dirs if d.lower() not in _PRUNE_DIRS]
        try:
            depth = len(Path(dirpath).relative_to(root).parts)
        except ValueError:
            continue
        depth_score = float(max(0, _GOOD_DEPTH - depth))
        mono = _is_mono_linux_dir(dirpath, files)
        for fname in files:
            full = os.path.join(dirpath, fname)
            kind = _classify(full, fname)
            if kind is None:
                continue
            yield kind, full, depth_score, mono and kind == _KIND_WINDOWS


def _is_mono_linux_dir(dirpath: str, files: list[str]) -> bool:
    """True when *dirpath* looks like a MonoKickstart Linux build.

    Two signals, either sufficient: a ``monoconfig``/``monomachineconfig``
    beside the assembly, or a bundled ``libmono-*.so*``. The ``lib64``/``lib``
    subdirectory holding that ``.so`` is pruned from the walk, so it is checked
    here by name rather than waited for.
    """
    lowered = {f.lower() for f in files}
    if any(marker in lowered for marker in _MONO_KICKSTART_MARKERS):
        return True
    base = Path(dirpath)
    return any(
        any((base / libdir).glob(_MONO_LIB_GLOB))
        for libdir in ("lib64", "lib")
        if (base / libdir).is_dir()
    )


def _classify(full: str, fname: str) -> str | None:
    """``"windows"``, ``"native"`` or None for a file that cannot be launched."""
    lowered = fname.lower()
    if lowered.endswith(".exe"):
        return _KIND_WINDOWS
    if lowered.endswith((".sh", ".appimage", ".x86_64", ".x86")):
        return _KIND_NATIVE
    if "." in lowered:
        # Anything else with an extension is data. Checked before the magic
        # probe so a 40 GB tree of .pak files costs one string test each.
        return None
    return _KIND_NATIVE if _is_native_runnable(full) else None


def _is_native_runnable(full: str) -> bool:
    """True for an extensionless file that is ``+x`` and an ELF or a script.

    The shebang half is not a nicety. A GOG Linux game's entry point is a bash
    script named after the game — ``game/Bastion``, no extension — so the
    ELF-only version of this probe returned None for it, the ``.exe`` beside it
    won by default, and the shortcut pointed at a Mono assembly under Proton.
    """
    try:
        if not os.access(full, os.X_OK) or not os.path.isfile(full):
            return False
        with open(full, "rb") as fh:
            header = fh.read(4)
    except OSError:
        return False
    return header == _ELF_MAGIC or header.startswith(_SHEBANG)


def _bonus(kind: str, full: str, title: str | None = None) -> float:
    """Size for Windows binaries, convention for native ones.

    Size is a good signal for a repack's ``.exe`` and a useless one for a
    three-line ``start.sh``, so the two kinds are scored on what actually
    distinguishes them.
    """
    name = os.path.basename(full).lower()
    if kind == _KIND_NATIVE and (
        name in _NATIVE_ENTRY_NAMES or _matches_title(name, title)
    ):
        return _MAX_SIZE_POINTS
    try:
        size = os.path.getsize(full)
    except OSError:
        size = 0
    return min(size / (_SIZE_CAP_MB * 1024 * 1024), _MAX_SIZE_POINTS)


def _matches_title(name: str, title: str | None) -> bool:
    """True when a native candidate is named after the game.

    GOG's Linux builds name the launcher after the game (``game/Bastion``)
    rather than shipping one of the conventional ``start.sh`` names, so without
    this the correct target scored no better than any other script in the tree.
    Compared on alphanumerics only, because the filename drops the punctuation
    and spacing a title carries.
    """
    if not title:
        return False
    stem = re.sub(r"[^a-z0-9]", "", Path(name).stem.lower())
    wanted = re.sub(r"[^a-z0-9]", "", title.lower())
    return bool(stem) and stem == wanted


def _looks_like_a_utility(full: str) -> bool:
    return any(k in os.path.basename(full).lower() for k in _UTIL_KEYWORDS)
