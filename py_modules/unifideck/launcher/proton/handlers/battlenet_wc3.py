"""Routing Warcraft III: Reforged onto a patched Proton at launch time.

py_modules/unifideck/launcher/proton/handlers/battlenet_wc3.py

Runs inside the out-of-process launcher, under the SYSTEM python (3.10 to
3.14), so this is stdlib-only and must not import the plugin backend.

This is the launch-time half of the workaround; the upstream Wine bug, why
the patched DLL has to live *inside* the Proton build, and the hardlink
trick that makes that cheap are all in
:mod:`~unifideck.launcher.proton.fixes.wc3_crypt32`. It sits in its own
module rather than in ``battlenet.py`` because that file is at its
volumetry cap.

**Why the redirect happens here and not in ``_build_umu_env``.** The env is
built once and handed to the plan, then phases A and C both read it — phase
C copies ``plan.env`` wholesale — so rewriting ``PROTONPATH`` on the plan
before the client starts covers the client *and* the game it spawns. Doing
it in the env builder would instead run the (filesystem) variant build on
every context construction; the launch log showed that happening three
times for a single launch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.game_title import resolve_title
from unifideck.launcher.proton.fixes import wc3_crypt32
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)


def apply_wc3_crypt32_fix(plan: ProtonLaunchPlan, family: str) -> None:
    """Point this launch at a Proton whose ``crypt32`` accepts 88 bytes.

    Best-effort and never fatal: when the fix cannot be applied the launch
    proceeds on the Proton that was selected, which means the game will show
    Blizzard's misleading VPN error. Say so plainly and name the one action
    that helps, because the game's own message sends people to their router.
    """
    if not wc3_crypt32.is_affected_title(family):
        return

    selected = plan.env.get("PROTONPATH")
    if not selected:
        logger.warning("[battlenet] no PROTONPATH to patch for Warcraft III")
        return

    proton = Path(selected)
    if wc3_crypt32.proton_has_crypt32_fix(proton):
        # The happy ending: Proton caught up and there is nothing to do.
        # GE-Proton11-7 was the first build measured carrying the backport.
        logger.info(
            "[battlenet] Warcraft III: %s already has the crypt32 fix",
            proton.name,
        )
        return

    variant = wc3_crypt32.ensure_patched_proton(
        plan.context.plugin_dir, proton,
    )
    if variant is None:
        _warn_unpatched(plan)
        return

    plan.env["PROTONPATH"] = str(variant)
    logger.info(
        "[battlenet] Warcraft III: using patched Proton %s", variant.name,
    )


def _warn_unpatched(plan: ProtonLaunchPlan) -> None:
    """Tell the user which Proton to pick, since we would not guess one."""
    launcher_toast(
        "toasts.launcher.wc3ProtonUnsupportedMessage",
        i18n_title_key="toasts.launcher.wc3ProtonUnsupported",
        game_title=resolve_title(plan.context.game_key),
        severity="error",
    )
