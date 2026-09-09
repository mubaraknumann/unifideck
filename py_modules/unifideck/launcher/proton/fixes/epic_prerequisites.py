"""epic_prerequisites.py — Run per-game prerequisite installers before legendary launch.

Some Epic titles ship a ``prereq_info`` block in legendary's
``installed.json`` pointing at a Windows installer (Ubisoft Connect
bootstrap, Visual C++ redistributables, …) that must run inside the
Wine prefix before the game itself launches. Mirrors Heroic's
``legendarySetup`` behaviour. Runs once per (game_id, prefix) pair —
guarded by a marker file inside the prefix so subsequent launches are
zero-cost.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.infrastructure.container_escape import (
    escape_argv,
)
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.prefix_layout import (
    normalize_prefix_root,
)

logger = logging.getLogger(__name__)
_INSTALLER_TIMEOUT_S = 600
_LEGENDARY_CONFIG_CANDIDATES = (
    Path("~/.config/legendary"),
    Path(
        "~/.var/app/com.heroicgameslauncher.hgl/config/heroic/"
        "legendaryConfig/legendary",
    ),
)

async def apply_epic_prerequisites(plan: ProtonLaunchPlan) -> bool:
    """Apply EPIC prerequisites.

    Returns True when the prereq step is satisfied (already done, no
    prereqs defined, or installer succeeded). Returns False only when
    a prereq exists and its installer failed — the caller may surface
    this but typically continues anyway, mirroring the legacy bash
    helper.
    """
    game_id = plan.context.game_id
    prefix_root = normalize_prefix_root(plan.prefix_path)
    new_marker, legacy_marker = _get_marker_paths(game_id, prefix_root)
    if new_marker.is_file() or legacy_marker.is_file():
        logger.debug(
            "[epic_prerequisites] %s: marker present, skipping",
            game_id,
        )
        return True
    prereq = _get_prereq_info(game_id)
    if prereq is None:
        _write_marker_sync(new_marker, body="no prerequisites")
        _cleanup_legacy_marker(legacy_marker)
        logger.info(
            "[epic_prerequisites] %s: no prereqs defined", game_id,
        )
        return True
    name = prereq.get("name") or "unknown"
    logger.info(
        "[epic_prerequisites] %s: installing %s", game_id, name,
    )
    launcher_toast(
        "toasts.launcher.installingPrerequisites",
        i18n_title_key="toasts.launcher.prerequisitesTitle",
        game_title=plan.context.game_key,
    )
    ok = await _run_prerequisite(plan, prereq, prefix_root)
    if ok:
        _write_marker_sync(new_marker, body=f"installed: {name}")
        _cleanup_legacy_marker(legacy_marker)
        logger.info(
            "[epic_prerequisites] %s: %s installed", game_id, name,
        )
        launcher_toast(
            "toasts.launcher.prerequisitesInstalled",
            i18n_title_key="toasts.launcher.prerequisitesReady",
            game_title=plan.context.game_key,
        )
    else:
        logger.warning(
            "[epic_prerequisites] %s: %s install failed", game_id, name,
        )
        launcher_toast(
            "toasts.launcher.prerequisitesFailedMessage",
            i18n_title_key="toasts.launcher.prerequisitesFailed",
            game_title=plan.context.game_key,
            severity="warning",
        )
    return ok

def _get_marker_paths(game_id: str, prefix_root: Path) -> tuple[Path, Path]:
    """Get marker paths.

    Returns (new_marker, legacy_marker). The new layout hashes the
    prefix into the filename so a renamed prefix doesn't reuse a
    stale marker.
    """
    # sha1 is used here purely as a fast filename hash to derive a
    # stable marker name from the prefix path — never as a security
    # primitive. ``usedforsecurity=False`` documents that intent to
    # both readers and FIPS-mode libraries.
    prefix_hash = hashlib.sha1(
        str(prefix_root).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    new_marker = prefix_root / (
        f".unifideck_prereqs_{game_id}_{prefix_hash}.done"
    )
    legacy_marker = prefix_root / f".unifideck_prereqs_{game_id}.done"
    return new_marker, legacy_marker

def _get_prereq_info(game_id: str) -> dict[str, Any] | None:
    """Get prereq info."""
    installed_json = _find_legendary_installed_json()
    if installed_json is None:
        logger.debug(
            "[epic_prerequisites] legendary installed.json not found",
        )
        return None
    try:
        with Path(installed_json).open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "[epic_prerequisites] installed.json read failed: %s", e,
        )
        return None
    entry = data.get(game_id) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    prereq = entry.get("prereq_info")
    if not isinstance(prereq, dict) or not prereq.get("path"):
        return None
    prereq = dict(prereq)
    prereq["install_path"] = entry.get("install_path", "")
    return prereq

def _find_legendary_installed_json() -> Path | None:
    """Find LEGENDARY installed JSON."""
    for candidate in _LEGENDARY_CONFIG_CANDIDATES:
        installed = candidate.expanduser() / "installed.json"
        if installed.is_file():
            return installed
    return None

async def _run_prerequisite(
    plan: ProtonLaunchPlan,
    prereq: dict[str, Any],
    prefix_root: Path,
) -> bool:
    """Run prerequisite."""
    install_path = prereq.get("install_path") or ""
    rel = prereq.get("path") or ""
    if not install_path or not rel:
        return False
    full_installer = Path(install_path) / rel
    if not full_installer.is_file():
        logger.warning(
            "[epic_prerequisites] installer missing: %s", full_installer,
        )
        return False
    env = _build_prereq_env(plan, prefix_root)
    cmd = _build_prereq_cmd(plan, full_installer, prereq)
    return await _spawn_installer(cmd, env)

def _build_prereq_env(
    plan: ProtonLaunchPlan, prefix_root: Path,
) -> dict[str, str]:
    """Build prereq env."""
    env = dict(plan.env)
    env["WINEPREFIX"] = str(prefix_root)
    env["GAMEID"] = "umu-0"
    env["PROTON_VERB"] = "waitforexitandrun"
    # Redundant with core.sanitize_frozen_loader_env/umu_runtime's own strip
    # since both were hardened against LD_PRELOAD leaks — kept as defense-in-
    # depth for this separate installer spawn point.
    env.pop("LD_PRELOAD", None)
    return env

def _build_prereq_cmd(
    plan: ProtonLaunchPlan,
    full_installer: Path,
    prereq: dict[str, Any],
) -> list[str]:
    """Build prereq cmd."""
    cmd = [str(plan.python_bin), str(plan.umu_wrapper), str(full_installer)]
    args = prereq.get("args") or ""
    if isinstance(args, str) and args.strip():
        cmd.extend(args.split())
    return cmd

async def _spawn_installer(cmd: list[str], env: dict[str, str]) -> bool:
    """Spawn installer.

    Many Windows installers exit non-zero for "already installed" or
    partial-success cases. We treat any termination (even non-zero) as
    success and rely on the game itself to fail at launch if the
    prereq is genuinely missing — same heuristic as the legacy helper.
    """
    # Escape Steam's pressure-vessel when Force-Compat wrapped us, or the
    # installer nests a second container and cannot run at all. No-op when
    # unwrapped. See infrastructure.container_escape.
    cmd = escape_argv(cmd, env, None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        logger.warning(
            "[epic_prerequisites] spawn failed: %s", e,
        )
        return False
    try:
        stdout, _err = await asyncio.wait_for(
            proc.communicate(), timeout=_INSTALLER_TIMEOUT_S,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        logger.warning(
            "[epic_prerequisites] installer timed out after %ds",
            _INSTALLER_TIMEOUT_S,
        )
        return False
    if stdout:
        _log_filtered_output(stdout.decode("utf-8", errors="replace"))
    return True

def _log_filtered_output(text: str) -> None:
    """Log filtered output."""
    markers = ("error", "warn", "install", "success", "fail", "complete", "info:")
    for line in text.splitlines():
        lower = line.lower()
        if any(m in lower for m in markers):
            logger.info("[epic_prerequisites]   %s", line.strip())

def _write_marker_sync(marker_path: Path, body: str) -> None:
    """Write marker sync."""
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(body, encoding="utf-8")
    except OSError as e:
        logger.debug(
            "[epic_prerequisites] marker write failed: %s", e,
        )

def _cleanup_legacy_marker(legacy_marker: Path) -> None:
    """Cleanup legacy marker."""
    if not legacy_marker.is_file():
        return
    with contextlib.suppress(OSError):
        Path(legacy_marker).unlink()
