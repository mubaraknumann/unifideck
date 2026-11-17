"""compat/winetricks.py — first-launch Windows redistributables.

Installs the redistributables a Windows game needs (VC++ runtimes,
d3dcompiler, mfc140, …) into its Proton prefix via
``umu-run winetricks <pkgs>``, exactly once per prefix (marker-guarded).
The package list comes from :mod:`game_fixes` — manual overrides →
umu-database protonfixes → global defaults. Generic: runs for every
Windows store, not just Epic.
"""
from __future__ import annotations

import logging
from pathlib import Path

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.fixes.game_fixes import get_required_winetricks
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

_MARKER_NAME = "unifideck_winetricks_complete.marker"
# Marker bodies that mean "don't run again" (terminal states).
_TERMINAL_MARKERS = ("complete", "no redistributables", "failed", "skipped")
# Bounds the winetricks step so a hung Proton/Wine (e.g. a broken
# auto-updated Proton-Experimental build spinning wineserver forever)
# can't wedge the serial install queue during prefix warmup. Generous:
# a cold prefix legitimately extracts several redistributables, but
# finite — the queue must survive a hang. On timeout the step is
# force-killed and treated as failed (the game still launches; the
# prefix finishes at launch).
_WINETRICKS_TIMEOUT_SECONDS = 300.0
# ``_command``'s answer when a tool is missing for now: no marker, no toast.
_DEFER = "defer"


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


def _already_done(marker: Path) -> bool:
    """True if a prior run reached a terminal state for this prefix."""
    if not marker.is_file():
        return False
    try:
        body = marker.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(m in body for m in _TERMINAL_MARKERS)


def _write_marker(marker: Path, body: str) -> None:
    """Best-effort marker write."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(body, encoding="utf-8")
    except OSError as e:
        logger.debug("[compat.winetricks] marker write failed: %s", e)


def proton_runs_winetricks(proton_dir: str | None) -> bool:
    """Whether ``umu-run winetricks`` can work with this Proton.

    umu execs ``<PROTONPATH>/protonfixes/winetricks``. GE-Proton and
    UMU-Proton bundle it; official Valve Protons ship no ``protonfixes``
    directory, so the verb dies inside umu with a FileNotFoundError after
    burning its whole timeout. False only for a Proton directory that exists
    and has no ``protonfixes/``; anything it cannot judge (no path, a path
    that is not there, an error) is True, so this only ever skips a run that
    was certain to fail.
    """
    if not proton_dir:
        return True
    try:
        root = Path(proton_dir)
        return not root.is_dir() or (root / "protonfixes").is_dir()
    except OSError:
        return True


def winetricks_pending(plan: ProtonLaunchPlan) -> bool:
    """Whether :func:`apply_winetricks` still has work for this prefix.

    Lets a caller decide whether it's worth arranging a winetricks-capable
    Proton at all, without running the step. A prefix that has never run it
    reports pending even if the package list would turn out empty — the step
    itself records that outcome in the marker on its first run.
    """
    return not _already_done(_prefix_root(plan) / _MARKER_NAME)


async def apply_winetricks(plan: ProtonLaunchPlan) -> bool:
    """Install required redistributables once per prefix.

    Best-effort: any failure writes a ``failed`` marker and returns —
    the caller continues to launch the game regardless. Returns ``True``
    only when the umu step was force-killed for exceeding its timeout
    (a hung Proton), so the warmup caller can retry with a good Proton.
    """
    prefix_root = _prefix_root(plan)
    marker = prefix_root / _MARKER_NAME
    if _already_done(marker):
        logger.debug(
            "[compat.winetricks] already done for %s", prefix_root,
        )
        return False

    packages = await get_required_winetricks(plan.context.game_id)
    if not packages:
        _write_marker(marker, "no redistributables")
        return False

    command = _command(plan, packages, prefix_root)
    if isinstance(command, str):  # _DEFER
        return False  # no marker: retried on the next launch
    if command is None:
        _write_marker(marker, "failed: no wine or winetricks to run")
        return False
    argv, env, attempts = command

    logger.info(
        "[compat.winetricks] installing for %s: %s",
        plan.context.game_id, ", ".join(packages),
    )
    _write_marker(marker, "installing: " + ", ".join(packages))
    launcher_toast(
        "toasts.launcher.installingRedistributables",
        i18n_title_key="toasts.header.gameSetup",
        game_title=plan.context.game_key,
    )

    try:
        rc = await run_umu_with_retry(
            argv, env=env, timeout=_WINETRICKS_TIMEOUT_SECONDS, max_attempts=attempts,
        )
    except Exception:
        logger.exception("[compat.winetricks] run failed")
        _write_marker(marker, "failed: exception")
        return False
    return _handle_winetricks_rc(plan, marker, rc)


def _command(
    plan: ProtonLaunchPlan, packages: list[str], prefix_root: Path,
) -> tuple[list[str], dict[str, str], int] | str | None:
    """``(argv, env, attempts)`` installing ``packages`` under the game's own Proton.

    GE-Proton and UMU-Proton: umu's winetricks verb, which runs their bundled
    winetricks. A Proton without one (Valve's): the plugin's winetricks with
    that Proton's Wine (``winetricks_wine``). Never another Proton: a prefix
    set up by two keeps symlinks into both. None when neither can run;
    :data:`_DEFER` when the step should wait for the next launch.

    The umu path gets the shared setup-helper env (GAMEID=umu-0,
    PROTON_VERB=run, no game-only sidecars — see infrastructure.setup_env),
    plus its own prefix pin and no runtime update so the redistributable
    install doesn't churn the umu runtime. Its umu-level retry is kept; the
    direct Wine run has no umu runtime to retry for.
    """
    proton_dir = plan.env.get("PROTONPATH")
    if proton_runs_winetricks(proton_dir):
        env = build_setup_env(plan)
        env["WINEPREFIX"] = str(prefix_root)
        env["UMU_RUNTIME_UPDATE"] = "0"
        argv = [
            str(plan.python_bin),
            str(plan.umu_wrapper),
            "winetricks",
            "-q",  # unattended — never block on a GUI prompt
            *packages,
        ]
        return argv, env, 2
    from unifideck.launcher.proton.compat import winetricks_wine

    cabextract = winetricks_wine.find_cabextract(plan.context.plugin_dir)
    if cabextract is None:
        # Three default packages (vcrun2022, mfc140, d3dcompiler_43) unpack
        # Microsoft's cab files and winetricks aborts the whole run without
        # it. A broken bundle is not this game's fault: try again next
        # launch rather than mark the prefix done without its libraries.
        logger.error(
            "[compat.winetricks] no working cabextract at %s or on the host; "
            "redistributables for %s wait for the next launch (reinstall the plugin)",
            plan.context.plugin_dir / "bin" / "cabextract" / "cabextract", plan.context.game_id,
        )
        return _DEFER
    proton = Path(str(proton_dir))
    copied = winetricks_wine.winsxs_links_to_copies(prefix_root, proton)
    logger.info(
        "[compat.winetricks] %s ships no winetricks; using its Wine with the bundled "
        "winetricks, cabextract %s (%d winsxs links copied into the prefix)",
        proton, cabextract, copied,
    )
    wine_command = winetricks_wine.command(plan, packages, proton, prefix_root, cabextract)
    return None if wine_command is None else (*wine_command, 1)


def _handle_winetricks_rc(
    plan: ProtonLaunchPlan, marker: Path, rc: int,
) -> bool:
    """Marker/toast bookkeeping for a winetricks rc; True iff it timed out."""
    if rc == 0:
        _write_marker(marker, "complete")
        logger.info(
            "[compat.winetricks] complete for %s", plan.context.game_id,
        )
        launcher_toast(
            "toasts.launcher.redistributablesInstalled",
            i18n_title_key="toasts.header.gameSetup",
            game_title=plan.context.game_key,
        )
        return False
    if rc == UMU_TIMEOUT_RC:
        # A timeout means the Proton/Wine boot hung (transient — usually
        # a broken auto-updated Proton build). Do NOT write a terminal
        # marker: once a good Proton is selected (see Part A), the next
        # launch should retry the redistributables install rather than
        # skip it forever on a stale "failed" marker.
        logger.warning(
            "[compat.winetricks] timed out for %s — leaving marker "
            "unwritten so a good Proton retries next launch",
            plan.context.game_id,
        )
        launcher_toast(
            "toasts.launcher.checkLogs",
            i18n_title_key="toasts.header.gameSetup",
            game_title=plan.context.game_key,
            severity="warning",
        )
        return True
    _write_marker(marker, f"failed: exit {rc}")
    logger.warning(
        "[compat.winetricks] rc=%d for %s",
        rc, plan.context.game_id,
    )
    launcher_toast(
        "toasts.launcher.checkLogs",
        i18n_title_key="toasts.header.gameSetup",
        game_title=plan.context.game_key,
        severity="warning",
    )
    return False
