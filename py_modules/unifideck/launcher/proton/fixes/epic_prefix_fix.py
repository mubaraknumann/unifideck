"""fixes/epic_prefix_fix.py — fake an Epic Games Launcher inside a prefix.

Some Ubisoft titles bundle EOS / anti-cheat checks that look for the Epic
Games Launcher, so the launch handler drops a wrapper exe where they look and
registers the ``com.epicgames.launcher`` protocol handler.

The registry half runs through umu (:func:`setup_run.run_setup_exe`), never a
Proton's ``files/bin/wine`` — see that module for why a bare Wine invocation
against a Proton prefix destroys the shared Proton install.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.prefix_layout import (
    normalize_prefix_root,
)
from unifideck.launcher.proton.infrastructure.setup_run import run_setup_exe

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)


def _copy_wrapper_to_drive_c(
    drive_c: Path,
    bundled_wrapper: Path,
    label: str,
) -> bool:

    """Copy wrapper to drive c."""
    if not bundled_wrapper.is_file():
        logger.warning(
            "[epic_prefix_fix] bundled wrapper missing at %s",
            bundled_wrapper,
        )
        return False
    copied = False
    epic_dir = (
        drive_c / "Program Files (x86)" / "Epic Games" / "Launcher"
        / "Portal" / "Binaries" / "Win32"
    )
    try:
        epic_dir.mkdir(parents=True, exist_ok=True)
        epic_target = epic_dir / "EpicGamesLauncher.exe"
        if epic_target.exists():
            with contextlib.suppress(OSError):
                epic_target.unlink()
        shutil.copy2(bundled_wrapper, epic_target)
        logger.info(
            "[epic_prefix_fix] copied wrapper to Epic dir (%s)",
            label,
        )
        copied = True
    except OSError as e:
        logger.warning(
            "[epic_prefix_fix] failed to copy to Epic dir "
            "(%s): %s",
            label, e,
        )
    win_command_dir = drive_c / "windows" / "command"
    try:
        win_command_dir.mkdir(parents=True, exist_ok=True)
        win_target = win_command_dir / "EpicGamesLauncher.exe"
        if win_target.exists():
            with contextlib.suppress(OSError):
                win_target.unlink()
        shutil.copy2(bundled_wrapper, win_target)
        logger.info(
            "[epic_prefix_fix] copied wrapper to "
            "windows/command (%s)",
            label,
        )
        copied = True
    except OSError as e:
        logger.warning(
            "[epic_prefix_fix] failed to copy to "
            "windows/command (%s): %s",
            label, e,
        )
    return copied


async def apply_epic_launcher_fix(
    plan: ProtonLaunchPlan,
    prefix_path: Path,
    bundled_wrapper: Path,
) -> bool:
    """Drop the wrapper exe into the prefix and register its protocol handler.

    The wrapper copy is the load-bearing half; the ``com.epicgames.launcher``
    protocol key is best-effort, so a failed registry step still returns
    ``True`` and the launch continues (the Ubisoft handler already has a
    no-EOS-key fallback).
    """
    prefix_root = normalize_prefix_root(prefix_path)
    root_drive_c = prefix_root / "drive_c"
    pfx_drive_c = prefix_root / "pfx" / "drive_c"
    found_any = False
    if root_drive_c.is_dir():
        _copy_wrapper_to_drive_c(root_drive_c, bundled_wrapper, "root")
        found_any = True
        if pfx_drive_c.is_dir():
            _copy_wrapper_to_drive_c(
                pfx_drive_c, bundled_wrapper, "pfx",
            )
            found_any = True
    if not found_any:
        logger.info(
            "[epic_prefix_fix] prefix not initialized yet, "
            "skipping",
        )
        return False
    registry_ok = await run_setup_exe(
        plan,
        "reg.exe",
        ["add", "HKEY_CLASSES_ROOT\\com.epicgames.launcher", "/f"],
        timeout_s=30,
        label="epic_prefix_fix",
    )
    if registry_ok:
        logger.info("[epic_prefix_fix] quick fix complete")
    else:
        logger.warning(
            "[epic_prefix_fix] quick fix completed with "
            "registry issues (non-fatal)",
        )
    return True
