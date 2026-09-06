"""compat/prefix_init.py — synchronous prefix creation + proton-change reset.

Runs once at the start of every Windows launch, before the compat
steps and the game. Two jobs (ported from staging's launcher
prefix-init block):

1. **Proton-change reset.** Each prefix records the Proton tool that
   built it (``.unifideck_proton_version``). When the resolved Proton
   *family* changes — the user switched Force Compatibility, unselected
   it (falling back to the latest GE-Proton), or moved between
   Experimental ↔ GE ↔ Proton 9/10 — the old Wine prefix is
   incompatible, so we back up its user data, wipe it + the setup
   markers, and toast ``protonUpgrade``/``resettingPrefix``. A
   same-family bump (e.g. GE-Proton10-10 → 10-34) keeps the prefix
   (Proton upgrades it in place) and just toasts ``protonSwitchedTo``.
   **Stores that install the game INSIDE the prefix are never reset** —
   see :func:`_prefix_owns_game_install`.

2. **First-time init.** If the prefix has no ``system.reg`` it isn't a
   usable Wine prefix yet, so we run ``umu-run createprefix`` (with
   retry) and toast ``firstTimeSetup``/``initializingPrefix`` →
   ``setupCompleteTitle``/``prefixInitialized``, falling back to
   ``wineboot --init`` (``setupFallback``) if createprefix doesn't
   produce a ``system.reg``.

Entirely best-effort: any failure is logged and the launch proceeds
(the game's own first umu run still initialises the prefix, exactly as
before this step existed).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.compat.ge_fallback import fallback_to_ge_proton
from unifideck.launcher.proton.compat.save_migration import (
    restore_or_migrate_saves,
)
from unifideck.launcher.proton.infrastructure.container_escape import (
    escape_argv,
)
from unifideck.launcher.proton.infrastructure.game_log import open_game_log
from unifideck.launcher.proton.infrastructure.prefix_layout import (
    normalize_prefix_root,
    resolve_registry_prefix,
)
from unifideck.launcher.proton.infrastructure.setup_env import build_setup_env
from unifideck.launcher.proton.infrastructure.umu_runtime import (
    ensure_umu_runtime_ready,
    repair_incomplete_umu_runtime,
)
from unifideck.launcher.wrapper_stores import (
    prefix_owns_game_install,
)

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

_MARKER_NAME = ".unifideck_proton_version"
# Kept across a prefix reset — everything else under the prefix root is
# Wine/Proton state or a re-runnable setup marker and gets wiped.
_PRESERVE = frozenset({_MARKER_NAME, ".save_backup"})
_CREATEPREFIX_ATTEMPTS = 3
_CREATEPREFIX_BACKOFF_SECONDS = 5
# Bounds a single umu-run step (createprefix / wineboot --init). Generous —
# legitimate first-time setup downloads the Steam Linux Runtime (hundreds of
# MB) — but finite: a hung Proton/Wine boot (confirmed live: a broken
# Proton-Experimental build spinning wineserver forever) must be killed
# rather than orphaned to run indefinitely.
_UMU_STEP_TIMEOUT_SECONDS = 120.0


def _prefix_owns_game_install(plan: ProtonLaunchPlan) -> bool:
    """True when the game's own files live INSIDE the prefix.

    True for the launcher-wrapper stores, where the vendor client runs
    in-prefix and installs there: Ubisoft to ``drive_c/Program Files
    (x86)/Ubisoft/Ubisoft Game Launcher/games/``, Battle.net to
    ``drive_c/Program Files (x86)/<Game>`` (confirmed on-device by a real
    Hearthstone install). Every other store downloads outside the prefix,
    so a reset there costs a rebuild; here it costs the user their game.

    The membership test lives in :mod:`launcher.proton.wrapper_stores` so
    this can never disagree with ``prefix_setup`` — that disagreement is
    precisely what caused the incident below.

    Confirmed live 2026-08-01: launching Rayman Origins resolved
    ``proton_experimental``, ``prefix_setup`` borrowed managed GE-Proton for
    umu's winetricks verb, this module saw experimental -> ge-proton and wiped
    the prefix — deleting the install. The borrow was for a step
    ``apply_prefix_compat`` skips for Ubisoft anyway.
    """
    return prefix_owns_game_install(getattr(plan.context, "store", ""))


def _proton_family(tool_id: str) -> str:
    """Coarse Proton family — a change here means the prefix must reset.

    The same family is spelled two different ways depending on where the id
    came from: the on-disk directory name (``GE-Proton11-3``) or Steam's
    compatibility-tool DISPLAY name, which the user picks in Properties >
    Compatibility (``Proton-GE Latest``). Matching raw substrings got both
    directions wrong, and each mistake is expensive — a false family change
    WIPES the prefix (see :func:`_should_reset_for_proton`).

    From a field bundle (2026-08-12/13) where one GOG prefix was reset six
    times in two days::

        Proton-GE Latest   -> GE-Proton11-3        family change -> RESET
        GE-Proton11-3      -> Proton-GE Latest     family change -> RESET
        Proton-CachyOS Latest -> Proton-GE Latest  "minor"       -> kept

    The first two are the SAME family (``Proton-GE Latest`` is a pointer to a
    GE-Proton build) and must never reset; the third is a genuine family
    change that was missed, because ``proton-ge latest`` and
    ``proton-cachyos latest`` both failed every check and collapsed to
    ``other``.

    So compare on an alphanumeric-only form, which makes ``GE-Proton11-3``
    and ``Proton-GE Latest`` both match ``geproton``/``protonge``. Order
    matters: ``umuproton904e`` also contains ``proton9``, so the named
    families are checked before the numeric ones.
    """
    t = tool_id.lower()
    normalized = "".join(ch for ch in t if ch.isalnum())
    if "experimental" in normalized:
        return "experimental"
    # CachyOS ships as "Proton-CachyOS Latest" / "cachyos-11.0-...-slr";
    # before this it fell through to "other" and silently shared a family
    # with every other display-name tool.
    if "cachyos" in normalized:
        return "cachyos"
    if "umuproton" in normalized or "protonumu" in normalized:
        return "umu-proton"
    if "geproton" in normalized or "protonge" in normalized:
        return "ge-proton"
    if "proton9" in t or "proton_9" in t or "proton 9" in t or "9.0" in t:
        return "proton9"
    if "proton10" in t or "proton_10" in t or "proton 10" in t or "10.0" in t:
        return "proton10"
    return "other"


async def ensure_prefix_initialized(plan: ProtonLaunchPlan) -> None:
    """Reset the prefix on a Proton family change, then create it if new."""
    try:
        prefix_root = normalize_prefix_root(plan.prefix_path)
        current = plan.state.proton_tool_id or "default"
        _handle_proton_change(plan, prefix_root, current)
        await _ensure_created(plan, prefix_root)
    except Exception:
        logger.exception("[prefix_init] prefix init/reset failed (non-fatal)")


def _read_previous_proton(prefix_root: Path) -> str | None:
    """The Proton tool that last built this prefix (our marker only).

    Deliberately does NOT fall back to Proton's own ``version`` file:
    on rollout, prefixes created before this feature have no marker, and
    we must not mass-reset working prefixes just because their Proton
    family differs from the new default. A missing marker → treat as a
    fresh baseline (record current, don't reset).
    """
    marker = prefix_root / _MARKER_NAME
    if not marker.is_file():
        return None
    try:
        return marker.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _should_reset_for_proton(
    plan: ProtonLaunchPlan, previous: str, current: str,
) -> bool:
    """Whether this Proton change warrants wiping the prefix. Logs why not.

    Only a *family* change makes the old Wine prefix incompatible; a
    same-family bump is upgraded in place by Proton itself. And even a family
    change is not worth a reset when the prefix holds the game install — see
    :func:`_prefix_owns_game_install`. Proton's ``wineboot -u`` migrates the
    prefix on the next umu run either way, which is what Steam does for a real
    app; a rebuilt prefix is recoverable, a deleted install is not.
    """
    if _proton_family(previous) == _proton_family(current):
        logger.info(
            "[prefix_init] minor Proton change %s -> %s; keeping prefix",
            previous, current,
        )
        return False
    if _prefix_owns_game_install(plan):
        logger.warning(
            "[prefix_init] Proton family change %s -> %s for %s, but the "
            "prefix holds the game install — NOT resetting; Proton will "
            "upgrade it in place",
            previous, current, plan.context.game_key,
        )
        return False
    logger.info(
        "[prefix_init] Proton family change %s -> %s; resetting prefix",
        previous, current,
    )
    return True


def _handle_proton_change(
    plan: ProtonLaunchPlan, prefix_root: Path, current: str,
) -> None:
    """Reset (major change) or notify (minor change); update the marker."""
    previous = _read_previous_proton(prefix_root)
    if previous and previous != current:
        if _should_reset_for_proton(plan, previous, current):
            launcher_toast(
                "toasts.launcher.resettingPrefix",
                i18n_title_key="toasts.launcher.protonUpgrade",
                i18n_params={"version": current},
                game_title=plan.context.game_key,
                severity="warning",
            )
            _reset_prefix(prefix_root)
        else:
            launcher_toast(
                "toasts.launcher.protonSwitchedTo",
                i18n_title_key="toasts.launcher.protonUpgrade",
                i18n_params={"version": current},
                game_title=plan.context.game_key,
            )
    with contextlib.suppress(OSError):
        prefix_root.mkdir(parents=True, exist_ok=True)
        (prefix_root / _MARKER_NAME).write_text(current, encoding="utf-8")


def _reset_prefix(prefix_root: Path) -> None:
    """Back up user data, then wipe the prefix (keeping our markers)."""
    active = resolve_registry_prefix(prefix_root)
    users = active / "drive_c" / "users"
    backup = prefix_root / ".save_backup"
    with contextlib.suppress(OSError):
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if users.is_dir():
            shutil.copytree(users, backup, dirs_exist_ok=True)
    # Remove all Wine/Proton state + re-runnable setup markers, leaving
    # only the proton-version marker and the save backup behind.
    for entry in _safe_iterdir(prefix_root):
        if entry.name in _PRESERVE:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()


def _safe_iterdir(path: Path) -> list[Path]:
    """``iterdir`` that returns ``[]`` instead of raising."""
    try:
        return list(path.iterdir())
    except OSError:
        return []


# Compat completion markers written by the per-prefix compat steps
# (compat/winetricks.py, compat/vcruntime.py). Cleared on a fresh
# createprefix so a stale one (from a failed setup) can't suppress the real
# install. The vcruntime marker is Proton-version-suffixed, so match by prefix.
_WINETRICKS_MARKER = "unifideck_winetricks_complete.marker"
_VCREG_MARKER_PREFIX = ".unifideck_vcreg_"


def _clear_stale_compat_markers(prefix_root: Path) -> None:
    """Delete compat 'done' markers (best-effort) before a fresh prefix build."""
    targets = [prefix_root / _WINETRICKS_MARKER]
    targets += [
        p for p in _safe_iterdir(prefix_root)
        if p.name.startswith(_VCREG_MARKER_PREFIX) and p.name.endswith(".done")
    ]
    for marker in targets:
        if marker.exists():
            with contextlib.suppress(OSError):
                marker.unlink()
                logger.info("[prefix_init] cleared stale compat marker %s", marker.name)



async def _ensure_created(plan: ProtonLaunchPlan, prefix_root: Path) -> None:
    """Run ``createprefix`` when the prefix has no ``system.reg`` yet."""
    if (resolve_registry_prefix(prefix_root) / "system.reg").is_file():
        logger.debug("[prefix_init] prefix already initialised: %s", prefix_root)
        return

    # Reaching here means there's no system.reg, so we're (re)building the
    # prefix from scratch — any compat "done" markers present are stale (left
    # by a prior *failed* attempt, e.g. the install-time warmup that crashed
    # before the loader-env fix and wrote bogus "complete" markers). Clear them
    # so apply_prefix_compat actually re-installs the redistributables.
    _clear_stale_compat_markers(prefix_root)

    logger.info("[prefix_init] initialising prefix %s", prefix_root)
    launcher_toast(
        "toasts.launcher.initializingPrefix",
        i18n_title_key="toasts.launcher.firstTimeSetup",
        game_title=plan.context.game_key,
    )
    # ``_handle_proton_change`` (called just before) already created the
    # prefix root; umu createprefix populates the Wine tree inside it.
    ensure_umu_runtime_ready()
    # Shared setup-helper env: GAMEID=umu-0 (generic — no per-game protonfix
    # during setup), PROTON_VERB=run rather than the inherited
    # ``waitforexitandrun`` whose ``wineserver -w`` caused the install-warmup
    # hang, and no game-only sidecars. ``infrastructure.setup_env`` carries the
    # canonical write-up of all three; this used to be it.
    env = build_setup_env(plan)

    if await _run_createprefix_with_retry(plan, env, prefix_root):
        await restore_or_migrate_saves(plan, prefix_root)
        launcher_toast(
            "toasts.launcher.prefixInitialized",
            i18n_title_key="toasts.launcher.setupCompleteTitle",
            game_title=plan.context.game_key,
        )
        return

    # Last resort — wineboot --init (createprefix never wrote system.reg).
    logger.warning("[prefix_init] createprefix failed; trying wineboot --init")
    launcher_toast(
        "toasts.launcher.fallbackInitialization",
        i18n_title_key="toasts.launcher.setupFallback",
        game_title=plan.context.game_key,
        severity="warning",
    )
    await _run_umu(plan, env, "wineboot", "--init")
    if (resolve_registry_prefix(prefix_root) / "system.reg").is_file():
        logger.info("[prefix_init] wineboot fallback initialised the prefix")
        await restore_or_migrate_saves(plan, prefix_root)
        return

    logger.warning(
        "[prefix_init] prefix still missing system.reg after createprefix "
        "+ wineboot fallback",
    )
    await fallback_to_ge_proton(plan, prefix_root)


async def _run_createprefix_with_retry(
    plan: ProtonLaunchPlan, env: dict[str, str], prefix_root: Path,
) -> bool:
    """Run ``umu-run createprefix`` until ``system.reg`` appears.

    Proton returns non-zero for ``createprefix`` even on success (it
    tries to "run" the keyword), so success is the presence of
    ``system.reg``, not the exit code.

    Between attempts this used to call ``cleanup_umu_runtime_cache()``,
    which deletes EVERY runtime variant in ``~/.local/share/umu``. Because
    the rc is meaningless here (see above), that nuke was unconditional: any
    prefix-init failure — the overwhelmingly common cause being environment
    or Proton problems, not runtime corruption — destroyed a perfectly good
    multi-hundred-MB shared runtime. It then retried 5 s later, far too
    little time to re-download, so attempt 2 failed for a NEW reason and
    nuked again, and attempt 3 ran with no runtime at all.

    That turned any transient launch bug into a permanently wedged install
    affecting *every* store (the runtime is shared): field bundles showed
    steamrt3 deleted outright and steamrt4 left as a partial download, after
    which even previously-working Epic games could not start, and each
    subsequent launch re-broke it. Diagnostics reported
    ``steamrt4 missing its entry point`` and umu died with
    ``FileNotFoundError: _v2-entry-point ... Runtime Platform missing or
    download incomplete``.

    :func:`repair_incomplete_umu_runtime` is the correct tool and is what
    runs here now: it removes ONLY a variant that is genuinely present-but-
    broken (payload extracted, ``umu`` entry-point symlink missing), leaves
    healthy siblings alone, and is a no-op on a healthy runtime — so umu
    re-downloads just what is actually broken, on its own schedule, instead
    of racing our retry timer.
    """
    wait = _CREATEPREFIX_BACKOFF_SECONDS
    for attempt in range(1, _CREATEPREFIX_ATTEMPTS + 1):
        logger.info(
            "[prefix_init] createprefix attempt %d/%d",
            attempt, _CREATEPREFIX_ATTEMPTS,
        )
        await _run_umu(plan, env, "createprefix")
        if (resolve_registry_prefix(prefix_root) / "system.reg").is_file():
            logger.info("[prefix_init] prefix created (system.reg present)")
            return True
        if attempt < _CREATEPREFIX_ATTEMPTS:
            launcher_toast(
                "toasts.launcher.retryingUmu",
                i18n_title_key="toasts.launcher.launchRetry",
                i18n_params={
                    "seconds": wait,
                    "attempt": attempt + 1,
                    "max": _CREATEPREFIX_ATTEMPTS,
                },
                severity="warning",
            )
            repair_incomplete_umu_runtime()
            await asyncio.sleep(wait)
            wait *= 2
    return False


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort SIGKILL of ``proc``'s whole process group.

    ``start_new_session=True`` makes the spawned umu-run its own
    session/process-group leader, so killing just ``proc.pid`` would
    leave every descendant running untouched — pressure-vessel,
    wineserver, the simulated services.exe/explorer.exe boot. That's
    exactly what left multiple hung createprefix trees running
    indefinitely (one over 30 minutes, still burning ~30% CPU) while
    diagnosing a broken Proton-Experimental build live.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as e:
        logger.warning("[prefix_init] failed to kill umu process group: %s", e)


def _reap_prefix_wineserver(env: dict[str, str]) -> None:
    """SIGKILL the detached wineserver bound to ``env``'s WINEPREFIX.

    The killpg above misses a ``waitforexitandrun`` wineserver — it
    detaches from the umu-run session and survives, holding the prefix's
    ``server-<dev>-<ino>/lock`` and deadlocking the next same-prefix run.
    Best-effort; no WINEPREFIX → nothing to reap.
    """
    prefix = env.get("WINEPREFIX")
    if not prefix:
        return
    try:
        from unifideck.launcher.proton.infrastructure.wineserver_reap import (
            reap_prefix_wineserver,
        )
        reap_prefix_wineserver(Path(prefix))
    except Exception:
        logger.exception("[prefix_init] wineserver reap failed for %s", prefix)


async def _run_umu(
    plan: ProtonLaunchPlan, env: dict[str, str], *umu_args: str,
) -> None:
    """Spawn ``<python> <umu-run> <args>`` and wait, teeing output to game.log.

    Runs in its own process group and is bounded by
    ``_UMU_STEP_TIMEOUT_SECONDS`` — a hung Proton/Wine boot is
    force-killed, process tree and all, instead of orphaned to run
    forever.

    Output used to go to ``DEVNULL``. That made every ``createprefix``
    failure completely undiagnosable: field bundles showed three silent
    attempts and "prefix still missing system.reg" with nothing whatsoever
    about *why* umu failed — the answer (``_v2-entry-point ... Runtime
    Platform missing or download incomplete``) was being thrown away. It
    goes to the same per-launch ``game.log`` the real launch uses, so a
    support bundle carries it.
    """
    # Force-Compat puts US inside Steam's pressure-vessel, and nesting a
    # second container makes umu die with "bwrap: execvp true: No such file
    # or directory" — every createprefix/wineboot exit 1, so the prefix is
    # never built and the launch cannot recover (field bundle 2026-08-12).
    # No-op when unwrapped. See infrastructure.container_escape.
    argv = escape_argv(
        [str(plan.python_bin), str(plan.umu_wrapper), *umu_args], env, None,
    )
    game_log = open_game_log()
    out = game_log if game_log is not None else asyncio.subprocess.DEVNULL
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    except OSError as e:
        logger.warning("[prefix_init] umu %s spawn failed: %s", umu_args, e)
        return
    finally:
        if game_log is not None:
            with contextlib.suppress(OSError):
                game_log.close()
    try:
        rc = await asyncio.wait_for(
            proc.wait(), timeout=_UMU_STEP_TIMEOUT_SECONDS,
        )
        if rc != 0:
            # Not necessarily a failure — Proton returns non-zero for
            # ``createprefix`` even when it works (see
            # _run_createprefix_with_retry). Logged at INFO so the code is
            # visible next to the game.log output without crying wolf.
            logger.info("[prefix_init] umu %s exit code: %d", umu_args, rc)
    except TimeoutError:
        logger.warning(
            "[prefix_init] umu %s exceeded %ds — killing process group",
            umu_args, int(_UMU_STEP_TIMEOUT_SECONDS),
        )
        _kill_process_group(proc)
        # killpg misses the detached wineserver: it survives holding this
        # prefix's /tmp/.wine-<uid>/server-<dev>-<ino>/lock, and the NEXT
        # step against the same prefix (compat, or a createprefix retry)
        # then deadlocks on it. Reap it so the retry gets a clean server.
        _reap_prefix_wineserver(env)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
