"""infrastructure/proton_health.py — spawn-time Proton sanity check.

Split out of ``umu_runtime`` (which the check pushed over the volumetry
file cap). One question, asked at the last possible moment: is the Proton
we are about to hand umu still intact?
"""
from __future__ import annotations

import logging
from pathlib import Path

from unifideck.launcher.types.errors import ProtonUnavailableError

from . import ge_installer, proton_integrity

logger = logging.getLogger(__name__)


def assert_proton_still_complete(env: dict[str, str] | None) -> None:
    """Re-check the selected Proton immediately before spawning umu.

    ``selector`` validates a Proton when it *picks* one, which was enough
    while every Proton we could pick was one we had installed ourselves
    into an immutable ``<tag>`` directory. Since 0.7.5 the default may be a
    tool an external manager owns and, per its own documentation, "updates
    in-place" — so the tree can be torn between selection and spawn.

    This is the failure ``is_proton_install_complete`` exists for: its
    docstring cites a broken *auto-updated* Proton wedging every
    ``umu-run`` operation's wineserver forever, and rests on the premise
    that "we download GE-Proton ourselves". External adoption removes that
    premise. Failing here costs a clear error instead of a hung wineserver.

    Deliberately unconditional rather than only for externally managed
    tools: it is a handful of stats plus two small reads, it needs no extra
    state threaded through ``proton_prepare``, and it equally covers an
    official Proton that Steam updated underneath a queued install.
    """
    proton_dir = (env or {}).get("PROTONPATH")
    if not proton_dir:
        return
    script = Path(proton_dir) / "proton"
    if not script.exists():
        # Not a Proton layout (umu resolves some PROTONPATH values itself).
        return
    problem = ge_installer.proton_install_problem(script)
    if problem:
        raise ProtonUnavailableError(
            f"Proton at {proton_dir} is unusable at launch time: {problem}",
        )


def check_after_failure(env: dict[str, str] | None) -> str | None:
    """After a failed umu run, check whether the Proton it used is damaged.

    A failed run is usually the game's own problem, and this leaves those
    alone: it reads a dozen DLL headers and stats the shipped file list
    (under half a second for ~8,000 entries), and does nothing when the
    tree is intact. When files GE shipped are missing or replaced, every
    game on this Proton will fail the same way, so it marks the tree (the next launch routes around it
    and the plugin's startup audit re-downloads it) and tells the user.
    Returns the problem it found. Never raises.
    """
    proton_dir = (env or {}).get("PROTONPATH")
    if not proton_dir:
        return None
    root = Path(proton_dir)
    try:
        if not (root / "proton").is_file() or proton_integrity.damaged_marker(root):
            return None
        problem = proton_integrity.quick_problem(root)
        if not problem:
            return None
        proton_integrity.mark_damaged(root, problem)
    except Exception:
        logger.warning("[launcher.proton] post-failure Proton check failed", exc_info=True)
        return None
    logger.warning(
        "[launcher.proton] %s is damaged (%s) — every game on it fails until it "
        "is re-downloaded; routing launches around it", root, problem,
    )
    try:
        from unifideck.launcher.frontend_bridge import launcher_toast

        launcher_toast(
            "toasts.launcher.protonDamagedMessage",
            i18n_title_key="toasts.launcher.protonDamaged",
            i18n_params={"version": root.name},
            severity="error",
        )
    except Exception:
        logger.debug("[launcher.proton] damaged-Proton toast failed", exc_info=True)
    return problem
