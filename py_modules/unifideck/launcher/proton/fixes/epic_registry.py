"""fixes/epic_registry.py — Epic/Uplay install keys for a Ubisoft prefix.

Writes the ``EpicGamesLauncher`` manifest keys (and the matching
``Ubisoft\\Launcher\\Installs`` entries when the title carries a ``-UplayId``)
that EOS-aware Ubisoft titles read to decide they are installed.

Every key goes in through umu (:func:`setup_run.run_setup_exe`), never a
Proton's ``files/bin/wine`` — see that module for why a bare Wine invocation
against a Proton prefix destroys the shared Proton install.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unifideck.launcher.proton.infrastructure.setup_run import run_setup_exe

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)
_UPLAY_ID_RE = re.compile(r"-UplayId=\s*(\d+)")
@dataclass(frozen=True)
class RegistryInjectionResult:
    """Registry injection result."""
    success: bool
    keys_written: int
    reason: str = ""


def _linux_to_wine_path(linux_path: str) -> str:
    """Linux to WINE path."""
    wine_path = "Z:" + linux_path.replace("/", "\\")
    if not wine_path.endswith("\\"):
        wine_path += "\\"
    return wine_path

def _load_installed_json(
    legendary_config: Path,
    game_id: str,
) -> dict[str, Any] | None:

    """Load installed JSON."""
    installed_json = legendary_config / "installed.json"
    if not installed_json.is_file():
        logger.error(
            "[epic_registry] installed.json not found at %s",
            installed_json,
        )
        return None
    try:
        with installed_json.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception("[epic_registry] failed to read installed.json")
        return None
    app = data.get(game_id)
    if not app:
        logger.error(
            "[epic_registry] game %s not in "
            "installed.json", game_id,
        )
        return None
    return cast("dict[Any, Any] | None", app)
def _build_reg_commands(
    game_id: str,
    wine_install_path: str,
    uplay_id: str | None,
) -> list[list[str]]:

    """Build reg commands."""
    commands: list[list[str]] = [
        [
            "add",
            "HKEY_LOCAL_MACHINE\\Software\\Epic Games\\EpicGamesLauncher",
            "/v", "AppDataPath", "/t", "REG_SZ",
            "/d", "C:\\ProgramData\\Epic\\EpicGamesLauncher\\Data\\",
            "/f",
        ],
        [
            "add",
            (
                "HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Epic Games"
                "\\EpicGamesLauncher\\Manifests\\" + game_id
            ),
            "/v", "InstallLocation", "/t", "REG_SZ",
            "/d", wine_install_path, "/f",
        ],
        [
            "add",
            (
                "HKEY_CURRENT_USER\\Software\\Epic Games"
                "\\EpicGamesLauncher\\Manifests\\" + game_id
            ),
            "/v", "InstallLocation", "/t", "REG_SZ",
            "/d", wine_install_path, "/f",
        ],
    ]
    if uplay_id:
        commands.extend([
            [
                "add",
                (
                    "HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Ubisoft"
                    "\\Launcher\\Installs\\" + uplay_id
                ),
                "/v", "InstallDir", "/t", "REG_SZ",
                "/d", wine_install_path, "/f",
            ],
            [
                "add",
                (
                    "HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Ubisoft"
                    "\\Launcher\\Installs\\" + uplay_id
                ),
                "/v", "Language", "/t", "REG_SZ",
                "/d", "en-US", "/f",
            ],
        ])
    return commands

async def _run_reg_commands(
    plan: ProtonLaunchPlan,
    commands: list[list[str]],
) -> int:
    """Apply each ``reg.exe add`` through umu; return how many succeeded.

    Serial, not gathered: they all write the same prefix registry, and a
    single wineserver serialises them anyway.
    """
    ok_count = 0
    for cmd in commands:
        ok = await run_setup_exe(
            plan, "reg.exe", cmd, timeout_s=30, label="epic_registry",
        )
        if ok:
            ok_count += 1
        else:
            logger.error("[epic_registry] reg add failed: %s", cmd[1])
    return ok_count
def _resolve_install_paths(
    app: dict[str, Any],
) -> tuple[str, str | None] | None:
    """Resolve install paths."""
    install_path = app.get("install_path")
    if not install_path:
        return None
    wine_install_path = _linux_to_wine_path(install_path)
    launch_params = app.get("launch_parameters", "") or ""
    uplay_match = _UPLAY_ID_RE.search(launch_params)
    uplay_id = uplay_match.group(1) if uplay_match else None
    return wine_install_path, uplay_id

def _error_result(reason: str) -> RegistryInjectionResult:

    """Error result."""
    return RegistryInjectionResult(
        success=False, keys_written=0, reason=reason,
    )


async def setup_registry(
    plan: ProtonLaunchPlan,
    game_id: str,
    legendary_config: Path,
) -> RegistryInjectionResult:
    """Write the Epic/Uplay install keys into the plan's prefix.

    The prefix is not a parameter: ``run_setup_exe`` runs inside the one
    ``plan.env`` already points at (``STEAM_COMPAT_DATA_PATH`` /
    ``PROTONPATH`` from ``proton_prepare``), which is the same prefix the
    real launch is about to use.
    """
    app = _load_installed_json(legendary_config, game_id)
    if app is None:
        return _error_result("installed_json_missing_or_unreadable")
    paths = _resolve_install_paths(app)
    if paths is None:
        logger.error(
            "[epic_registry] no install_path for %s", game_id,
        )
        return _error_result("no_install_path")
    wine_install_path, uplay_id = paths
    commands = _build_reg_commands(
        game_id=game_id,
        wine_install_path=wine_install_path,
        uplay_id=uplay_id,
    )
    ok_count = await _run_reg_commands(plan, commands)
    total = len(commands)
    all_ok = ok_count == total
    logger.info(
        "[epic_registry] setup for %s (uplay=%s): %d/%d keys",
        game_id, uplay_id, ok_count, total,
    )
    return RegistryInjectionResult(
        success=all_ok,
        keys_written=ok_count,
        reason="" if all_ok else "partial_reg_add_failures",
    )
