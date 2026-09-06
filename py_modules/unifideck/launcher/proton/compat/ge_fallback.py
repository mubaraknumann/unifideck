"""launcher/proton/compat/ge_fallback.py — last-resort GE-Proton fallback.

Split out of ``prefix_init.py`` (was pushing it over the volumetry file
cap). ``select_proton_version`` honors the user's Steam-wide
global-default compat tool (tier 4) even when that specific build is
broken — confirmed live while testing the 0.6.1 -> 0.7.1 upgrade: a
Proton-Experimental snapshot spun ``wineserver`` forever inside
``createprefix``, while GE-Proton succeeded in ~9s against the
identical prefix. This module is the "give the resolved tool a fair
chance first, then fall back to bundled GE-Proton" last resort.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.infrastructure.core import proton_prepare

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)


def _resolve_ge_proton() -> tuple[Path, str] | None:
    """Latest cached/installed GE-Proton, downloading it if necessary."""
    from unifideck.launcher.proton.infrastructure import ge_installer

    cached_tag = ge_installer.read_cached_latest_tag()
    if cached_tag:
        path = ge_installer.installed_ge_proton_path(cached_tag)
        if path:
            return path, cached_tag
    return ge_installer.ensure_latest_ge()


async def fallback_to_ge_proton(
    plan: ProtonLaunchPlan, prefix_root: Path,
) -> None:
    """Last resort: retry prefix creation with the bundled latest GE-Proton.

    Only reached once every attempt with the originally-resolved tool
    (createprefix x3 + wineboot --init) has already failed — "give it a
    fair chance first," not an immediate bail. GE-Proton is Unifideck's
    own bundled, known-good default, so it's the one sane fallback.

    Already-GE-Proton attempts have nothing further to fall back to and
    are skipped. On success the fallback is persisted as this game's
    Force-Compat choice (tier 1 in ``select_proton_version``) so the
    very next launch uses GE-Proton directly instead of re-resolving the
    broken tool and hanging again — and the proton-version marker is
    re-stamped so this prefix isn't immediately reset as a "family
    change" the next time it's launched.
    """
    from unifideck.launcher.proton.compat.prefix_init import (
        _MARKER_NAME,
        _run_createprefix_with_retry,
    )
    from unifideck.launcher.proton.compat.save_migration import (
        restore_or_migrate_saves,
    )

    resolved = _resolve_ge_proton()
    if resolved is None:
        logger.warning("[prefix_init] GE-Proton fallback unavailable (offline?)")
        return
    ge_path, tag = resolved

    current_tool = plan.state.proton_tool_id or ""
    current_proton = getattr(plan.state, "proton_path", None)
    if current_tool == tag or (current_proton and current_proton == ge_path):
        logger.warning(
            "[prefix_init] already on fallback GE-Proton (%s); no further fallback",
            current_tool,
        )
        return

    logger.warning(
        "[prefix_init] %s failed to create a usable prefix; "
        "falling back to bundled GE-Proton %s",
        current_tool, tag,
    )
    ge_plan = proton_prepare(
        plan.context, plan.state,
        python_bin=plan.python_bin,
        proton_path=ge_path,
        proton_tool_id=tag,
        on_process_start=plan.on_process_start,
    )
    ge_env = dict(ge_plan.env)
    ge_env["GAMEID"] = "umu-0"

    if not await _run_createprefix_with_retry(ge_plan, ge_env, prefix_root):
        logger.warning("[prefix_init] GE-Proton fallback also failed")
        return

    # Re-stamp the marker so the next launch doesn't see a "family change"
    # (e.g. experimental -> ge-proton) and reset the prefix just built.
    with contextlib.suppress(OSError):
        (prefix_root / _MARKER_NAME).write_text(tag, encoding="utf-8")
    from unifideck.compatibility.proton_helpers import save_proton_setting
    save_proton_setting(f"{plan.context.store}:{plan.context.game_id}", tag)

    await restore_or_migrate_saves(ge_plan, prefix_root)
    launcher_toast(
        "toasts.launcher.protonSwitchedTo",
        i18n_title_key="toasts.launcher.protonUpgrade",
        i18n_params={"version": tag},
        game_title=plan.context.game_key,
    )
