"""compat/epic.py — Epic-specific launch compatibility.

Pieces that only apply to the Epic/legendary launch path:

* :func:`resolve_legendary_config_path` — locate the authenticated
  legendary config (local, then the Heroic flatpak's), used both for
  ``LEGENDARY_CONFIG_PATH`` and to find the EOS overlay install.
* :func:`detect_offline` — Steam offline mode / no connectivity, so the
  launch can pass ``--offline`` to legendary.
* :func:`apply_eos_overlay` — install the EOS (Epic Online Services)
  overlay via legendary and enable it for the game's prefix, mirroring
  Heroic. Required by some titles (e.g. Football Manager). Also mirrors
  ``OverlayPath`` into ``system.reg`` (HKLM) since some EOS SDK builds
  probe HKLM while legendary only writes HKCU (user.reg).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from pathlib import Path

from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

_LEGENDARY_CONFIG_CANDIDATES = (
    Path("~/.config/legendary"),
    Path(
        "~/.var/app/com.heroicgameslauncher.hgl/config/heroic/"
        "legendaryConfig/legendary",
    ),
)
_LEGENDARY_TIMEOUT_S = 120


def resolve_legendary_config_path() -> str:
    """Return the authenticated legendary config dir, or ``""``.

    Prefers the local config; falls back to Heroic's. Authentication
    is detected by the presence of ``user.json``.
    """
    for candidate in _LEGENDARY_CONFIG_CANDIDATES:
        expanded = candidate.expanduser()
        if (expanded / "user.json").is_file():
            return str(expanded)
    return ""


def resolve_legendary_bin(plugin_dir: Path) -> str:
    """Resolve the legendary binary — bundled first, then PATH.

    Bare ``legendary`` isn't on PATH in the launcher's scrubbed env, so
    prefer the plugin-bundled copy (an env override wins if set).
    """
    bundled = plugin_dir / "bin" / "legendary"
    return os.environ.get("LEGENDARY_BIN") or (
        str(bundled) if bundled.is_file() else "legendary"
    )


def build_legendary_env(
    plan: ProtonLaunchPlan, config_path: str,
) -> dict[str, str]:
    """Build the env for the legendary launch.

    ``STORE=none`` (umu shouldn't apply a store profile to legendary
    itself), drop any stale ``LEGENDARY_WRAPPER_EXE`` (set only for the
    Ubisoft-on-Epic path), tag the Heroic app runner, and point at the
    authenticated legendary config (auth + EOS overlay registry).
    """
    env = dict(plan.env)
    # STORE=none keeps umu from applying an egs profile to the legendary
    # wrapper chain — correct for every Epic title EXCEPT Rockstar-on-Epic
    # (RDR2/GTA5), which needs the egs profile umu-run picks up (set in
    # core.proton_prepare). Preserve whatever proton_prepare chose for
    # those; force "none" for all others.
    from unifideck.launcher.proton.fixes.game_fixes import is_rockstar_egs
    if not is_rockstar_egs(
        plan.context.game_id, plan.state.umu_id, plan.context.exe_path.name,
    ):
        env["STORE"] = "none"
    env.pop("LEGENDARY_WRAPPER_EXE", None)
    env["HEROIC_APP_RUNNER"] = "legendary"
    if config_path:
        env["LEGENDARY_CONFIG_PATH"] = config_path
    return env


def detect_offline() -> bool:
    """True when Steam is in offline mode or there's no connectivity.

    Steam's offline mode (``steam_offline.py``), then a fast TCP probe (no
    ``ping``/``curl`` dependency). Any error → assume online (return False).
    """
    from unifideck.launcher.steam_offline import steam_offline_mode
    if steam_offline_mode():
        logger.info("[compat.epic] Steam offline mode (loginusers)")
        return True
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=3):
            return False
    except OSError:
        logger.info("[compat.epic] no network connectivity detected")
        return True


def _active_wineprefix(plan: ProtonLaunchPlan) -> Path:
    """Return the initialised Wine prefix (``pfx`` subdir or root)."""
    root = plan.prefix_path.resolve()
    while root.name == "pfx":
        root = root.parent
    pfx = root / "pfx"
    if (pfx / "drive_c").is_dir() or (pfx / "user.reg").is_file():
        return pfx
    return root


async def _run_legendary(
    legendary_bin: str, args: list[str], config_path: str,
) -> int:
    """Run a legendary subcommand with the resolved config path."""
    env = dict(os.environ)
    if config_path:
        env["LEGENDARY_CONFIG_PATH"] = config_path
    try:
        proc = await asyncio.create_subprocess_exec(
            legendary_bin, *args, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), timeout=_LEGENDARY_TIMEOUT_S)
    except (OSError, TimeoutError):
        logger.warning(
            "[compat.epic] legendary %s failed/timed out", args[:2],
        )
        with contextlib.suppress(Exception):
            proc.kill()
        return 1


def _mirror_overlay_path(active_prefix: Path, config_path: str) -> None:
    """Mirror the overlay's ``OverlayPath`` into ``system.reg`` (HKLM).

    legendary only writes HKCU (user.reg); some EOS SDK builds probe
    HKLM. Idempotent — skips if the key is already present.
    """
    overlay_json = Path(config_path) / "overlay_install.json"
    system_reg = active_prefix / "system.reg"
    if not overlay_json.is_file() or not system_reg.is_file():
        return
    try:
        install_path = json.loads(
            overlay_json.read_text(encoding="utf-8"),
        ).get("install_path", "")
        if not install_path:
            return
        existing = system_reg.read_text(encoding="utf-8", errors="replace")
        if "Epic Games\\\\EOS" in existing and "OverlayPath" in existing:
            return
        block = (
            f"\n[Software\\\\Epic Games\\\\EOS] {int(time.time())}\n"
            f'"OverlayPath"="Z:{install_path}"\n'
        )
        with system_reg.open("a", encoding="utf-8") as fh:
            fh.write(block)
        logger.info("[compat.epic] mirrored OverlayPath into system.reg")
    except (OSError, ValueError) as e:
        logger.debug("[compat.epic] OverlayPath mirror skipped: %s", e)


async def apply_eos_overlay(
    plan: ProtonLaunchPlan, legendary_bin: str, config_path: str,
) -> None:
    """Install (once) + enable the EOS overlay for this game's prefix."""
    # Install globally if not present yet.
    overlay_installed = (
        config_path
        and (Path(config_path) / "overlay_install.json").is_file()
    )
    if not overlay_installed:
        logger.info("[compat.epic] installing EOS overlay")
        await _run_legendary(
            legendary_bin, ["eos-overlay", "install", "-y"], config_path,
        )

    # ALWAYS enable for the prefix — never skip. Some titles refuse to
    # start ("Failed to connect to the Epic Launcher") without the
    # overlay's EOS IPC. The prefix is created by the earlier
    # regedit/compat step; if it isn't there yet, ensure the dir exists so
    # legendary can write the overlay registry keys (it creates user.reg).
    active_prefix = _active_wineprefix(plan)
    with contextlib.suppress(OSError):
        active_prefix.mkdir(parents=True, exist_ok=True)
    await _run_legendary(
        legendary_bin,
        ["eos-overlay", "enable", "--prefix", str(active_prefix)],
        config_path,
    )
    _mirror_overlay_path(active_prefix, config_path)
