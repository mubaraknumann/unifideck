"""compat/vcruntime.py — VC++ runtime registry fix for UE4-class titles.

UE4 launcher stubs call ``MsiQueryProductState`` to verify VC++ is
installed; winetricks copies the DLLs but doesn't populate the MSI
product database, and Proton rewrites ``system.reg`` on prefix
upgrades — erasing text-injected keys. This imports a bundled ``.reg``
via ``umu-run regedit`` *after* Proton has initialised the prefix
(winetricks runs first in :mod:`compat`). The marker is keyed to the
Proton tool name so it re-runs when the user switches Proton. Generic
across stores; best-effort.
"""
from __future__ import annotations

import logging
from pathlib import Path

from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.prefix_layout import (
    normalize_prefix_root,
)
from unifideck.launcher.proton.infrastructure.setup_env import build_setup_env
from unifideck.launcher.proton.infrastructure.umu_runtime import (
    UMU_TIMEOUT_RC,
    run_umu_with_retry,
)

logger = logging.getLogger(__name__)

# Bounds the regedit import so a hung Proton/Wine (e.g. a broken
# auto-updated Proton-Experimental build spinning wineserver forever)
# can't wedge the serial install queue during prefix warmup. This is a
# trivial 4-key registry import that finishes in well under a second on
# a healthy prefix — a generous bound here only ever fires on a genuine
# hang. On timeout no ``.done`` marker is written (only rc==0 writes
# one), so a good Proton retries it next launch.
_VCRUNTIME_TIMEOUT_SECONDS = 120.0

# One marker, whose BODY is Proton's own prefix stamp at import time.
#
# It used to be one marker per Proton tool id (``.unifideck_vcreg_<tool>.v2``),
# which could not see the case that actually erases the keys: any Proton other
# than the one we imported under running ``wineboot -u`` on the prefix
# ("Upgrading prefix from X to Y") and rewriting ``system.reg``. The old marker
# for OUR tool was still sitting there, so the import was skipped forever and
# the prefix stayed permanently without the keys. Comparing against Proton's
# ``version`` file instead detects every rewrite — a user Proton switch, or a
# Proton Steam handed the game directly — because Proton restamps it each time.
#
# Name keeps the ``.unifideck_vcreg_`` prefix so ``prefix_init``'s
# ``_clear_stale_compat_markers`` still matches it on a fresh createprefix.
_MARKER_NAME = ".unifideck_vcreg_v3.done"


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


def _prefix_proton_stamp(plan: ProtonLaunchPlan, prefix_root: Path) -> str:
    """Proton's ``version`` stamp for this prefix, or the tool id if absent.

    This is the value Proton prints in its "Upgrading prefix from X to Y"
    line. It changes on every prefix create/upgrade, which is exactly when
    the imported keys are lost.
    """
    try:
        stamp = (prefix_root / "version").read_text(
            encoding="utf-8", errors="replace",
        ).strip()
    except OSError:
        stamp = ""
    return stamp or (plan.state.proton_tool_id or "unknown")


def _drop_legacy_markers(prefix_root: Path) -> None:
    """Remove the old per-Proton-tool markers superseded by :data:`_MARKER_NAME`."""
    for old in prefix_root.glob(".unifideck_vcreg_*.done"):
        if old.name == _MARKER_NAME:
            continue
        try:
            old.unlink()
        except OSError as e:
            logger.debug("[compat.vcruntime] stale marker unlink failed: %s", e)


def vcruntime_fix_pending(plan: ProtonLaunchPlan) -> bool:
    """Whether the VC++ keys still need importing into this prefix."""
    prefix_root = _prefix_root(plan)
    try:
        body = (prefix_root / _MARKER_NAME).read_text(
            encoding="utf-8", errors="replace",
        ).strip()
    except OSError:
        return True
    return body != _prefix_proton_stamp(plan, prefix_root)


def _wine_z_path(linux_path: Path) -> str:
    """Map an absolute Linux path to its Wine ``Z:`` drive path.

    Wine always maps ``Z:\\`` to ``/``, so we can hand regedit the
    bundled .reg directly — no copy into ``drive_c`` and no guessing
    which prefix layout umu used for ``C:`` (the bug that produced
    ``regedit: The file 'C:\\vcruntime_fix.reg' was not found``).
    """
    return "Z:" + str(linux_path).replace("/", "\\")


async def apply_vcruntime_fix(plan: ProtonLaunchPlan) -> bool:
    """Import the bundled VC++ runtime keys once per (prefix, Proton).

    Returns ``True`` only when the umu regedit step was force-killed for
    exceeding its timeout (a hung Proton), so the warmup caller can
    retry with a good Proton. A ``.done`` marker is written only on a
    clean ``rc == 0``, so a timeout is naturally retried next launch.
    """
    reg_file = plan.context.plugin_dir / "bin" / "vcruntime_fix.reg"
    if not reg_file.is_file():
        return False
    prefix_root = _prefix_root(plan)
    proton_name = plan.state.proton_tool_id or "unknown"
    if not vcruntime_fix_pending(plan):
        return False
    marker = prefix_root / _MARKER_NAME

    # Shared setup-helper env: GAMEID=umu-0 and PROTON_VERB=run (the inherited
    # ``waitforexitandrun`` would deadlock against the wineserver left by the
    # earlier createprefix/winetricks step), minus any game-only sidecar.
    # See infrastructure.setup_env for the full explanation.
    env = build_setup_env(plan)
    # ``/S`` imports silently — no GUI dialog on success OR error. The
    # error dialog (when C: path was wrong) blocked the launch for as
    # long as it stayed open; the Z: path + /S removes that entirely.
    argv = [
        str(plan.python_bin),
        str(plan.umu_wrapper),
        "regedit",
        "/S",
        _wine_z_path(reg_file),
    ]
    rc = 1
    try:
        rc = await run_umu_with_retry(
            argv, env=env, timeout=_VCRUNTIME_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("[compat.vcruntime] regedit run failed")

    if rc == 0:
        # Read the stamp AFTER the run, not before: this regedit is itself a
        # umu-run, so if it was the first invocation under a newly-selected
        # Proton it has just triggered that Proton's prefix upgrade. Recording
        # the post-upgrade stamp is what makes the next launch a no-op instead
        # of re-importing forever.
        stamp = _prefix_proton_stamp(plan, prefix_root)
        _drop_legacy_markers(prefix_root)
        try:
            marker.write_text(stamp, encoding="utf-8")
        except OSError as e:
            logger.debug("[compat.vcruntime] marker write failed: %s", e)
        logger.info(
            "[compat.vcruntime] imported for proton=%s (prefix stamp %s)",
            proton_name, stamp,
        )
        return False
    if rc == UMU_TIMEOUT_RC:
        logger.warning(
            "[compat.vcruntime] regedit timed out (proton=%s hung) — "
            "no marker written, will retry with a good Proton",
            proton_name,
        )
        return True
    logger.warning("[compat.vcruntime] regedit rc=%d", rc)
    return False
