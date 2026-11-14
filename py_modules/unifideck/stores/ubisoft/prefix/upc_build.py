"""
Keep the ``.template`` prefix on the newest Ubisoft Connect build we have.

Every game prefix is a clone of ``.template``, and the template's UPC is
frozen at the build it was created with. A clone therefore starts on an
old UPC, which patches itself on first launch: a restart, a windowless
patch run, then the new build. Measured 2026-11-14: a 13316 clone patched
itself to 175.1.13416, and the new build signed out the session it had
been handed. A clone of a template that already carries 13416 started
signed in, with no restart.

UPC only updates itself where it runs: in ``.upc-auth`` when the user
signs in, or in a game prefix during an install or a launch. So the
newest build on disk lives in one of those, and we copy its program
files into the template before cloning, and back after an install.
Only the program files are copied, never ``games/``, saves, logs or the
user's session, and never from or to a prefix whose UPC is running.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_UBISOFT_DIR = Path("drive_c/Program Files (x86)/Ubisoft")
_LAUNCHER_DIR = "Ubisoft Game Launcher"
_VERSION_FILE = "version.txt"
#: Per-prefix or per-user content inside the UPC program dir.
_NOT_PROGRAM_FILES = ("games", "logs", "crashes", "savegames", "data")


def ubisoft_dir(prefix: str | Path) -> Path | None:
    """``…/Program Files (x86)/Ubisoft`` in either prefix layout."""
    base = Path(prefix)
    for root in (base, base / "pfx"):
        candidate = root / _UBISOFT_DIR
        if (candidate / _LAUNCHER_DIR).is_dir():
            return candidate
    return None


def upc_build(prefix: str | Path) -> int | None:
    """The UPC build installed in ``prefix`` (``version.txt``), or None."""
    root = ubisoft_dir(prefix)
    if root is None:
        return None
    try:
        text = (root / _LAUNCHER_DIR / _VERSION_FILE).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _upc_running(prefix: str | Path) -> bool:
    from unifideck.launcher.proton.handlers.wrapper_clients import (
        client_running_in,
    )

    try:
        return client_running_in("ubisoft", prefix)
    except Exception:
        logger.exception("[UbisoftUpcBuild] process scan failed for %s", prefix)
        return True  # unknown: treat as busy, never copy a live install


async def copy_upc_build(src_prefix: str | Path, dst_prefix: str | Path) -> bool:
    """Copy ``src_prefix``'s UPC program files over ``dst_prefix``'s.

    Refuses when UPC is running in either prefix (a half-written update or
    files in use) and when it would not be an upgrade.
    """
    src_build, dst_build = upc_build(src_prefix), upc_build(dst_prefix)
    src_root, dst_root = ubisoft_dir(src_prefix), ubisoft_dir(dst_prefix)
    if src_build is None or src_root is None or dst_root is None:
        return False
    if dst_build is not None and dst_build >= src_build:
        return False
    for prefix in (src_prefix, dst_prefix):
        if await asyncio.to_thread(_upc_running, prefix):
            logger.info(
                "[UbisoftUpcBuild] UPC is running in %s — not copying build %s",
                Path(prefix).name, src_build,
            )
            return False
    excludes = [f"--exclude=/{_LAUNCHER_DIR}/{d}/" for d in _NOT_PROGRAM_FILES]
    # ``--checksum``: build numbers are the same length ("13316"/"13416"), so
    # rsync's size+mtime quick check can skip a changed file. This only runs
    # when the builds differ, so the full read is paid once per UPC update.
    args = ["rsync", "-a", "--checksum", *excludes, f"{src_root}/", f"{dst_root}/"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except (OSError, ValueError):
        logger.exception("[UbisoftUpcBuild] rsync spawn failed")
        return False
    if proc.returncode != 0:
        logger.warning(
            "[UbisoftUpcBuild] copying build %s %s → %s failed (rc=%s): %s",
            src_build, Path(src_prefix).name, Path(dst_prefix).name,
            proc.returncode, err.decode(errors="replace").strip()[:300],
        )
        return False
    logger.info(
        "[UbisoftUpcBuild] %s: Ubisoft Connect %s → %s (from %s)",
        Path(dst_prefix).name, dst_build, src_build, Path(src_prefix).name,
    )
    return True


async def refresh_template_build(
    template: str | Path, sources: list[str],
) -> bool:
    """Bring ``template`` up to the newest UPC build among ``sources``.

    Never raises: a template on an old build still works, it just patches
    itself on first launch.
    """
    try:
        builds = [(upc_build(s), s) for s in sources]
        candidates = sorted(
            ((b, s) for b, s in builds if b is not None), reverse=True,
        )
        current = upc_build(template)
        for build, source in candidates:
            if current is not None and build <= current:
                return False
            if await copy_upc_build(source, template):
                return True
    except Exception:
        logger.exception("[UbisoftUpcBuild] template refresh failed")
    return False
