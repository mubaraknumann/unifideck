"""Warcraft III: Reforged's Proton check at launch time.

py_modules/unifideck/launcher/proton/handlers/battlenet_wc3.py

Runs inside the out-of-process launcher, under the SYSTEM python (3.10 to
3.14), so this is stdlib-only and must not import the plugin backend.

The upstream Wine bug and why Unifideck no longer patches around it are in
:mod:`~unifideck.launcher.proton.fixes.wc3_crypt32`. It sits in its own
module rather than in ``battlenet.py`` because that file is at its
volumetry cap.
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
    """Warn when the selected Proton cannot sign Warcraft III in.

    Never fatal: the launch proceeds on the selected Proton either way. When
    that Proton lacks the fix the game will show Blizzard's misleading VPN
    error, so say so plainly and name the one action that helps, because
    the game's own message sends people to their router.
    """
    if not wc3_crypt32.is_affected_title(family):
        return
    try:
        wc3_crypt32.remove_stale_variants()
    except OSError as exc:
        logger.warning("[battlenet] could not remove old Warcraft III variants: %s", exc)
    selected = plan.env.get("PROTONPATH")
    if not selected:
        logger.warning("[battlenet] no PROTONPATH to check for Warcraft III")
        return
    proton = Path(selected)
    if wc3_crypt32.proton_has_crypt32_fix(proton):
        logger.info(
            "[battlenet] Warcraft III: %s already has the crypt32 fix",
            proton.name,
        )
        return
    logger.warning(
        "[battlenet] Warcraft III: %s lacks the crypt32 fix; the game will "
        "report a VPN error until GE-Proton11-7 or newer is selected",
        proton.name,
    )
    launcher_toast(
        "toasts.launcher.wc3ProtonUnsupportedMessage",
        i18n_title_key="toasts.launcher.wc3ProtonUnsupported",
        game_title=resolve_title(plan.context.game_key),
        severity="error",
    )
