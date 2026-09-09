"""compat/gog_setup — GOG first-launch redistributable + script setup.

Faithful port of Heroic's ``setup.ts`` (staging's ``gog_setup.py``),
adapted to the launcher architecture and split for LOC control:

* :mod:`common`  — paths, manifest loaders, wine exec, lang map
* :mod:`redist`  — download (gogdl) + install redistributables
* :mod:`scripts` — scriptinterpreter / temp_executable + goggame-*.script

``apply_gog_setup(plan, language)`` is the single entry, called once per
prefix (marker-guarded) from the GOG handler before launch. Standalone
— imports nothing from ``unifideck.stores`` (the launcher's slim Python
can't load the GOG store's cryptography chain). Best-effort: failures
log and never block the launch, except a hard redistributable-install
failure which is surfaced to the caller.
"""
from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from unifideck.launcher.proton.infrastructure.prefix_layout import (
    normalize_prefix_root,
)

from .common import (
    get_dependencies,
    load_manifest,
    load_redist_manifest,
    wait_for_prefix_ready,
)
from .redist import ensure_redist_downloaded, install_redistributables
from .scripts import (
    apply_script_registry,
    run_script_interpreter,
    run_temp_executable,
)

if TYPE_CHECKING:
    from pathlib import Path

    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

# ``.v2`` invalidates markers written by the builds whose ``AUTH_CONFIG``
# pointed at a ``gogdl/auth.json`` subdir that never existed: those runs logged
# "cannot download redist (gogdl=True auth=False)", installed NOTHING, and then
# wrote this marker anyway — latching the failure for the life of the prefix.
# Every affected prefix retries its redistributables exactly once on the next
# launch. Bump again if the redist step's contract changes.
_MARKER_NAME = ".unifideck-gog-setup-done.v2"
# Versioned marker for the script-registry step alone. Bumped to ``.v2`` when
# the dual-WOW64-view write landed; existing prefixes (whose only marker is the
# heavy ``_MARKER_NAME``) re-apply their registry script once so 32-bit GOG
# titles stop showing "Install". Bump the suffix again if the write changes.
_REG_MARKER = ".unifideck-gog-script-reg.v2"


def _prefix_root(plan: ProtonLaunchPlan) -> Path:
    """The prefix root for *plan*, with any trailing ``pfx`` stripped.

    A four-line wrapper over the canonical
    ``prefix_layout.normalize_prefix_root``. This module, ``vcruntime``
    and ``winetricks`` each held a byte-identical copy of that logic
    beside it, and three more lived in ``proton/fixes/`` under the
    canonical name — six copies of a helper that was already promoted.
    Check 11 could not see them: it matches by name, and these were
    renamed (audit register items 20 and 47).
    """
    return normalize_prefix_root(plan.prefix_path)


async def apply_gog_setup(
    plan: ProtonLaunchPlan, language: str = "en-US",
) -> None:
    """Install GOG redistributables + apply setup-script registry once."""
    prefix_root = _prefix_root(plan)
    game_id = plan.context.game_id
    install_path = str(plan.context.work_dir or plan.context.exe_path.parent)

    if not wait_for_prefix_ready(prefix_root):
        return  # prefix not initialised yet; next launch retries

    # Always ensure the game's setRegistry script is applied (both WOW64 views)
    # — guarded by its own versioned marker, INDEPENDENT of the heavy-setup
    # marker below, so prefixes built before the dual-view fix self-heal.
    await _ensure_script_registry(plan, game_id, install_path, prefix_root)

    marker = prefix_root / _MARKER_NAME
    if marker.is_file():
        logger.debug("[gog_setup] heavy setup already done for %s", prefix_root)
        return

    manifest = load_manifest(game_id)
    if manifest is None:
        _write_marker(marker)
        logger.info("[gog_setup] complete for %s (no manifest)", game_id)
        return

    deps = get_dependencies(manifest)
    logger.info("[gog_setup] %s deps: %s", game_id, ", ".join(deps) or "none")
    redists_ok = True
    if deps:
        redists_ok = await ensure_redist_downloaded(plan, deps)
    await _run_setup_scripts(plan, game_id, manifest, install_path, language)
    if deps and redists_ok:
        redists_ok = await _install_redists(plan, deps)
    if not redists_ok:
        # Do NOT write the marker: the game's declared redistributables
        # (MSVC*, UE4REDIST, …) are what its launcher stub checks for, and a
        # marker here would suppress the retry forever — which is exactly how
        # every GOG prefix ended up with no redistributables at all. Still
        # best-effort: the launch continues either way.
        logger.warning(
            "[gog_setup] redistributables incomplete for %s — leaving marker "
            "unwritten so the next launch retries", game_id,
        )
        return
    _write_marker(marker)
    logger.info("[gog_setup] complete for %s", game_id)


async def _run_setup_scripts(
    plan: ProtonLaunchPlan,
    game_id: str,
    manifest: dict[str, Any],
    install_path: str,
    language: str,
) -> None:
    """Run the v2 scriptInterpreter / temp-executable setup step, if any."""
    if manifest.get("version") != 2:
        return
    if manifest.get("scriptInterpreter"):
        await run_script_interpreter(
            plan, game_id, manifest, install_path, language,
        )
    else:
        await run_temp_executable(
            plan, game_id, manifest, install_path, language,
        )


async def _install_redists(plan: ProtonLaunchPlan, deps: list[str]) -> bool:
    """Install downloaded redistributables; False if the manifest is absent.

    A missing manifest means the download never landed, so the caller must
    not record the setup as done.
    """
    redist_manifest = load_redist_manifest()
    if redist_manifest is None:
        logger.warning("[gog_setup] no redist manifest found")
        return False
    await install_redistributables(plan, deps, redist_manifest)
    return True


async def _ensure_script_registry(
    plan: ProtonLaunchPlan, game_id: str, install_path: str, prefix_root: Path,
) -> None:
    """Apply ``goggame-*.script`` setRegistry actions once (versioned marker).

    Separate from the heavy-setup marker so existing prefixes re-apply their
    registry script (now to both WOW64 views) exactly once on next launch.
    Idempotent: ``reg.exe add /f`` overwrites, so a re-run is harmless.
    """
    reg_marker = prefix_root / _REG_MARKER
    if reg_marker.is_file():
        return
    await apply_script_registry(plan, game_id, install_path)
    _write_marker(reg_marker)
    logger.info("[gog_setup] script registry applied for %s", game_id)


def _write_marker(marker: Path) -> None:
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("done", encoding="utf-8")


__all__ = ["apply_gog_setup"]
