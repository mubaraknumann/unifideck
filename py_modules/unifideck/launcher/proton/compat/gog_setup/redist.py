"""compat/gog_setup/redist.py — download + install GOG redistributables.

Ports Heroic ``setup.ts`` redistributable handling: download the
manifest-declared deps via gogdl (plus ``ISI``, the script interpreter)
and install each into the Wine prefix. Best-effort; mirrors staging.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.frontend_bridge import launcher_toast

from .common import AUTH_CONFIG, REDIST_DIR, run_wine

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)


def _gogdl_bin(plan: ProtonLaunchPlan) -> Path:
    return plan.context.plugin_dir / "bin" / "gogdl"


def _missing_deps(all_deps: list[str]) -> list[str]:
    """Requested deps with no populated directory under ``__redist``."""
    redist_base = REDIST_DIR / "__redist"
    return [
        d for d in all_deps
        if not (redist_base / d).is_dir()
        or not any((redist_base / d).iterdir())
    ]


async def ensure_redist_downloaded(
    plan: ProtonLaunchPlan, deps: list[str],
) -> bool:
    """Download missing redistributables (and ISI) via gogdl.

    Returns ``True`` only when every requested dep is actually on disk
    afterwards, so the caller can decline to write its "done" marker and
    retry next launch instead of latching a failure forever.
    """
    all_deps = ["ISI"] + [d for d in deps if d != "ISI"]
    REDIST_DIR.mkdir(parents=True, exist_ok=True)
    missing = _missing_deps(all_deps)
    if not missing:
        logger.info("[gog_setup] all redistributables already present")
        return True

    launcher_toast(
        "toasts.launcher.installingRedistMessage",
        i18n_title_key="toasts.header.gameSetup",
        game_title=plan.context.game_key,
    )
    gogdl = _gogdl_bin(plan)
    if not gogdl.is_file() or not AUTH_CONFIG.is_file():
        logger.warning(
            "[gog_setup] cannot download redist (gogdl=%s auth=%s at %s)",
            gogdl.is_file(), AUTH_CONFIG.is_file(), AUTH_CONFIG,
        )
        return False

    # Serialise downloads across concurrent launches with a file lock.
    lock_path = REDIST_DIR / ".download.lock"
    with lock_path.open("w") as lock:
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        await _run_redist_download(gogdl, missing)

    # gogdl's exit code alone is not proof it wrote anything — re-check the
    # tree it was supposed to fill before declaring the deps satisfied.
    still_missing = _missing_deps(all_deps)
    if still_missing:
        logger.warning(
            "[gog_setup] redist still missing after download: %s",
            ", ".join(still_missing),
        )
        return False
    return True


async def _run_redist_download(gogdl: Path, missing: list[str]) -> None:
    """Invoke gogdl to download ``missing`` redistributables into REDIST_DIR."""
    logger.info("[gog_setup] downloading redist: %s", ", ".join(missing))
    cmd = [
        str(gogdl), "--auth-config-path", str(AUTH_CONFIG),
        "redist", "--ids", ",".join(missing), "--path", str(REDIST_DIR),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "[gog_setup] redist download rc=%d: %s",
                proc.returncode,
                (err or b"").decode("utf-8", "replace")[:300],
            )
    except OSError as e:
        logger.warning("[gog_setup] redist download spawn failed: %s", e)


def _find_depot(
    redist_manifest: dict[str, Any], dep: str,
) -> dict[str, Any] | None:
    for d in redist_manifest.get("depots", []) or []:
        if isinstance(d, dict) and d.get("dependencyId") == dep:
            return d
    return None


async def install_redistributables(
    plan: ProtonLaunchPlan,
    deps: list[str],
    redist_manifest: dict[str, Any],
) -> None:
    """Install each prefix-targeted redistributable via wine/proton."""
    logger.info("[gog_setup] installing %d redistributable(s)", len(deps))
    for dep in deps:
        depot = _find_depot(redist_manifest, dep)
        if depot is None:
            logger.info("[gog_setup] %s not in redist manifest, skipping", dep)
            continue
        exe_info = depot.get("executable") or {}
        rel = exe_info.get("path") or ""
        # Only redistributables that install into the prefix (``__redist``)
        # — ones that drop into the game dir are handled by the game.
        if not rel or not rel.startswith("__redist"):
            continue
        exe_path = REDIST_DIR / rel
        if not exe_path.is_file():
            logger.warning("[gog_setup] redist exe missing: %s", exe_path)
            continue
        args = shlex.split(exe_info.get("arguments") or "")
        name = depot.get("readableName", dep)
        logger.info("[gog_setup] installing %s (%s)", name, dep)
        # PHYSXLEGACY ships as an .msi — drive it through msiexec.
        if dep == "PHYSXLEGACY":
            await run_wine(plan, "msiexec", ["/i", str(exe_path), "/qb"])
        else:
            await run_wine(plan, str(exe_path), args)
