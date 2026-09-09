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
    from unifideck.launcher.proton.infrastructure import ge_installer, ge_marker

    cached_tag = ge_marker.read_cached_latest_tag()
    if cached_tag:
        path = ge_installer.installed_ge_proton_path(cached_tag)
        if path:
            return path, cached_tag
    return ge_installer.ensure_latest_ge()


def _same_proton(a: Path | None, b: Path | None) -> bool:
    """True when both paths name the same ``proton`` script after resolving.

    An externally managed GE is routinely an *alias directory* symlinked at a
    build we already have, so ``Proton-GE Latest/proton`` and
    ``GE-Proton11-6/proton`` compare unequal as ``Path`` objects while being
    one file. Measured on a Steam Deck 2026-09-07: without resolving, the
    caller's "already on the fallback" guard missed, and the fallback re-ran
    createprefix x3 plus ``wineboot --init`` against the identical Proton
    that had just failed. A path that cannot be resolved (broken symlink,
    permission) falls back to raw comparison rather than raising.
    """
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


async def fallback_to_ge_proton(
    plan: ProtonLaunchPlan, prefix_root: Path,
) -> None:
    """Last resort: retry prefix creation with the bundled latest GE-Proton.

    Only reached once every attempt with the originally-resolved tool
    (createprefix x3 + wineboot --init) has already failed — "give it a
    fair chance first," not an immediate bail. GE-Proton is Unifideck's
    own bundled, known-good default, so it's the one sane fallback.

    Skipped only when the tool that just failed **is** the fallback — same
    tag, or the same ``proton`` script after resolving symlinks. It is
    deliberately an identity test and not a family test: an externally
    managed GE (ProtonPlus's ``Proton-GE Latest``) is in the ``ge-proton``
    family but is a *different build we do not control*, so a family test
    would refuse the one fallback that could still help. Resolving matters
    too, because such a tool is often an alias directory symlinked at the
    very build we fall back to, and comparing raw paths would retry the
    identical Proton that just failed.

    On success the fallback is persisted as this game's
    Force-Compat choice (tier 1 in ``select_proton_version``) so the
    very next launch uses GE-Proton directly instead of re-resolving the
    broken tool and hanging again — and the proton-version marker is
    re-stamped so this prefix isn't immediately reset as a "family
    change" the next time it's launched.
    """
    from unifideck.launcher.proton.compat.prefix_init import (
        _run_createprefix_with_retry,
    )

    resolved = _resolve_ge_proton()
    if resolved is None:
        logger.warning("[prefix_init] GE-Proton fallback unavailable (offline?)")
        return
    ge_path, tag = resolved

    current_tool = plan.state.proton_tool_id or ""
    current_proton = plan.state.proton_path
    if current_tool == tag or _same_proton(current_proton, ge_path):
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

    await _persist_fallback_choice(plan, ge_plan, prefix_root, tag)


async def _persist_fallback_choice(
    plan: ProtonLaunchPlan,
    ge_plan: ProtonLaunchPlan,
    prefix_root: Path,
    tag: str,
) -> None:
    """Make the successful fallback stick, and tell the user.

    Pins ``tag`` as this game's Force-Compat choice (tier 1) so the next
    launch goes straight to it instead of re-resolving the broken tool and
    hanging again, and re-stamps the prefix marker so that next launch does
    not read the switch as a family change and reset the prefix just built.
    """
    from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME
    from unifideck.launcher.proton.compat.save_migration import (
        restore_or_migrate_saves,
    )

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
