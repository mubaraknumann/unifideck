from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.infrastructure.game_log import (
    open_game_log,
)

from .container_escape import escape_argv
from .proton_health import assert_proton_still_complete

logger = logging.getLogger(__name__)
# Short pause before a recoverable umu retry — gives a transient
# runtime/network hiccup a moment to clear and makes the "retrying in
# Ns" toast truthful.
_RETRY_BACKOFF_SECONDS = 3
UMU_CACHE_DIR = Path("~/.local/share/umu").expanduser()
# umu-run picks the Steam Runtime *generation* per Proton build (it reads
# the selected PROTONPATH's own toolmanifest.vdf) — a newer GE-Proton can
# require "steamrt4" instead of the default "sniper"/"steamrt3". Mirrors
# the variant names in umu's own bundled ``umu/__init__.py``
# (``__runtime_versions__``). Managing only "steamrt3" here made our own
# cache checks/wipes a silent no-op for anyone on a build that resolved to
# a different variant — covering all three keeps them meaningful
# regardless of which runtime a given Proton build actually uses.
UMU_RUNTIME_VARIANTS = ("steamrt2", "steamrt3", "steamrt4", "steamrt4-arm64")
# Name of the marker umu >=1.4.0 writes inside a runtime variant directory
# once, and only once, ``check_runtime()`` has validated the extracted payload
# (see ``umu/umu_util.py``'s ``write_install_marker`` in the bundled zipapp).
# It is umu's OWN definition of "this runtime finished installing", which
# makes it strictly better evidence than probing for the entry point — see
# :func:`_runtime_entry_point_ok`.
_UMU_INSTALL_MARKER = ".installed.ok"
# How long a failed repair suppresses another attempt on the same variant.
# Deliberately an on-disk marker with a TTL rather than in-memory state: the
# prefix warmup runs in the long-lived Decky BACKEND while launches run in a
# fresh launcher process, and both repair the SAME shared runtime — so
# process-scoped state would both miss the cross-process case and, in the
# backend, disable UD-084 self-heal permanently after one unrelated failure.
# The TTL means a genuinely fixable runtime starts self-healing again.
#
# Short (2 min) because under the bundled umu a wipe IS recoverable: umu
# re-downloads the variant on the next run. The TTL only has to outlast the
# retries of a single launch so one launch cannot spin; anything longer just
# means a transient failure (network blip, disk full) keeps failing launches
# after the cause has cleared. It was 10 min when a wiped runtime could never
# come back — see :func:`repair_incomplete_umu_runtime`.
_REPAIR_MARKER_TTL_SECONDS = 120
_RECOVERABLE_CODES = {2, 74, 127}
# A recoverable code only means "the runtime/launch failed to come up" when
# it arrives quickly. Past this many seconds the process demonstrably RAN,
# so the code is the game's own exit status and retrying would relaunch a
# game the user just quit — and, for 2/74, wipe the shared umu runtime that
# every other game depends on.
#
# This became load-bearing with UD-126: Epic used to report legendary's exit
# code (always 0, retries dead), and now reports the game's, like every other
# store. Chosen generously — nothing legitimately spends two minutes failing
# to bootstrap a runtime, and a game quit inside two minutes with exactly
# rc 2/74/127 costs one harmless relaunch attempt.
_RECOVERABLE_MAX_RUNTIME_SECONDS = 120
# Recoverable codes whose likely cause is a corrupt/incomplete steamrt
# runtime bootstrap — the only ones that justify wiping the *shared*
# runtime cache (hundreds of MB, re-downloaded on the next launch of
# ANY game) before a retry. 127 (command-not-found) is recoverable but
# is NOT a runtime-corruption signal, so it retries WITHOUT the
# expensive nuke. Wiping the shared cache on every recoverable failure
# was both wasteful and — paired with the old "Network Error" title —
# actively misleading about the real failure.
_RUNTIME_CORRUPTION_CODES = {2, 74}
# Returned when a bounded umu step is force-killed for exceeding its
# timeout. Never in ``_RECOVERABLE_CODES`` on purpose: a hung
# Proton/Wine boot (e.g. a broken auto-updated Proton-Experimental
# build spinning wineserver forever) will just hang again on retry, so
# the caller should fail the step rather than loop.
UMU_TIMEOUT_RC = 124


def _now() -> float:
    """Monotonic clock, indirected so tests can script attempt durations.

    ``run_umu_with_retry`` times each attempt to tell a runtime that
    failed to bootstrap from a game that ran and then exited with the
    same code (see :data:`_RECOVERABLE_MAX_RUNTIME_SECONDS`). Patching
    ``time.monotonic`` itself would also move the event loop's clock.
    """
    return time.monotonic()


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort SIGKILL of ``proc``'s whole process group.

    ``start_new_session=True`` (see :func:`_run_umu_once`) makes the
    spawned umu-run its own session/process-group leader, so killing
    just ``proc.pid`` would leave every descendant running untouched —
    pressure-vessel, wineserver, the simulated Wine boot. A broken
    Proton build left exactly such trees spinning wineserver at ~14%
    CPU indefinitely, wedging the serial install queue. Killing the
    group reaps the whole tree.

    Mirrors ``prefix_init._kill_process_group`` (the createprefix path
    already does this); kept as a local copy to avoid an import-linter
    layer dependency from ``infrastructure`` onto ``compat``.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as e:
        logger.warning("[launcher.umu] failed to kill process group: %s", e)


def _reap_prefix_wineserver(env: dict[str, str] | None) -> None:
    """Also SIGKILL the wineserver bound to ``env``'s WINEPREFIX.

    ``_kill_process_group`` misses it: a ``waitforexitandrun`` wineserver
    detaches from the umu-run session and survives the killpg, keeping its
    ``/tmp/.wine-<uid>/server-<dev>-<ino>/lock``. Left alive, it deadlocks
    the NEXT run against the same prefix (retries stack stuck wineservers
    on one lock — the observed install-warmup wedge). Reaping it here lets
    the retry get a clean server. Best-effort; no WINEPREFIX → nothing to do.
    """
    prefix = (env or {}).get("WINEPREFIX")
    if not prefix:
        return
    try:
        from unifideck.launcher.proton.infrastructure.wineserver_reap import (
            reap_prefix_wineserver,
        )
        reap_prefix_wineserver(Path(prefix))
    except Exception:
        logger.exception("[launcher.umu] wineserver reap failed for %s", prefix)


def cleanup_umu_runtime_cache() -> None:
    """Cleanup UMU runtime cache."""
    targets = [
        *(UMU_CACHE_DIR / variant for variant in UMU_RUNTIME_VARIANTS),
        UMU_CACHE_DIR / "compatibilitytool.vdf",
        UMU_CACHE_DIR / ".ref",
    ]
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            with contextlib.suppress(OSError):
                target.unlink()
    logger.info("[launcher.umu] cache cleaned: %s", UMU_CACHE_DIR)
def _runtime_entry_point_ok(variant_dir: Path) -> bool:
    """Return whether ``variant_dir`` has a usable umu entry point.

    Mirrors umu's OWN launch-time gate (``build_command`` does
    ``entry_point.is_file()`` on ``<variant>/umu``): the ``umu`` symlink must
    resolve to an existing ``_v2-entry-point``. ``Path.is_file()`` follows
    symlinks, so a *missing* ``umu`` file AND a *dangling* ``umu`` symlink
    both return ``False`` — exactly the two states that make umu raise
    "Runtime Platform missing or download incomplete" *after* it has already
    logged "<variant> is up to date".

    Why umu's own ``.installed.ok`` marker is deliberately NOT accepted as an
    alternative signal here, despite being newer and more authoritative about
    whether an install *finished*: the two answer different questions.

    * ``.installed.ok`` (written only when ``check_runtime`` validated the
      mtree) is what ``has_umu_setup`` reads, so it means "umu will NOT
      reinstall this".
    * ``<variant>/umu`` is what umu must actually exec.

    Treating them as interchangeable (``marker or entry_point``) reopens
    UD-084 in its 1.4.x form: umu writes the marker and then creates the
    symlink in a ``finally:``, so a runtime can carry the marker with a
    broken entry point. umu would decline to reinstall it (marker present)
    and then fail to exec it — the precise wedge this repair exists to break,
    and we would have declared it healthy.

    The reverse mismatch needs no handling: a runtime with an entry point but
    NO marker is one umu will reinstall by itself on the next run, so it is
    not our problem — and pre-1.4 runtimes, which have no marker at all, must
    not be wiped for lacking one.

    Our own ``.unifideck-repair-<variant>`` marker lives in the cache ROOT,
    not inside a variant dir, so it can never be confused with umu's.
    """
    return (variant_dir / "umu").is_file()
def _repair_marker(variant: str) -> Path:
    """Path of the "we already tried repairing this" marker for ``variant``."""
    return UMU_CACHE_DIR / f".unifideck-repair-{variant}"


def _repair_recently_attempted(variant: str) -> bool:
    """Whether ``variant`` was repaired within the marker TTL."""
    try:
        age = time.time() - _repair_marker(variant).stat().st_mtime
    except OSError:
        return False
    return age < _REPAIR_MARKER_TTL_SECONDS


def runtime_is_unrecoverable(variant: str) -> bool:
    """True when ``variant`` is broken AND a recent repair already failed.

    See :func:`repair_incomplete_umu_runtime` for why a second failure is
    treated as terminal rather than retried.
    """
    variant_dir = UMU_CACHE_DIR / variant
    return (
        _repair_recently_attempted(variant)
        and variant_dir.is_dir()
        and not _runtime_entry_point_ok(variant_dir)
    )


def unrecoverable_runtime_variants() -> list[str]:
    """Every variant that survived a repair still broken, for error text."""
    return [v for v in UMU_RUNTIME_VARIANTS if runtime_is_unrecoverable(v)]


def repair_incomplete_umu_runtime() -> None:
    """Wipe any runtime variant that is present but has no umu entry point.

    UD-084: a umu setup that died after extracting the runtime payload
    (``<variant>_platform_*`` / ``pressure-vessel`` / ``VERSIONS.txt``) but
    before its LAST step — creating the ``umu -> _v2-entry-point`` symlink
    (umu's ``_install_umu`` does that in a ``finally``) — leaves a runtime
    that umu's own ``_update_umu`` treats as "up to date" (it only checks
    the platform dir / pressure-vessel / VERSIONS.txt, never the entry
    point). The next launch then dies in ``build_command`` with
    ``FileNotFoundError``, which umu exits with a code OUTSIDE our
    ``_RECOVERABLE_CODES`` (0 when the bundled zipapp swallows it, 1 when a
    field build re-raises) — so ``run_umu_with_retry`` never retries or
    wipes, and the user stays wedged.

    Deleting just the broken variant dir lets umu re-download it cleanly on
    the next ``umu-run`` this same launch; healthy sibling variants are left
    untouched (surgical). Cheap — one ``stat`` per existing variant — and a
    no-op on a healthy runtime, so it is safe to call on every launch. Safe
    without locking: launches run serially and this runs before any umu
    process is spawned, so no concurrent umu holds ``umu.lock`` here.

    ONCE PER VARIANT PER TTL. The repair assumes umu can re-download what we
    delete, and that assumption is only as good as the bundled umu.

    It was FALSE up to umu 1.4.1, which fetched from
    ``repo.steampowered.com/<variant>/images/latest-public-beta[/VERSION.txt]``
    — ``latest-*`` symlink paths the repo answers with HTTP 403 (numbered
    version dirs still return 200). umu's *update* path treats a 403 as
    non-fatal and keeps the runtime it already has, but its *install* path
    RAISES, so once a variant was gone it could never come back; re-deleting
    the stub umu left behind merely spun, and field logs showed this fire
    three times in a single launch.

    The bundled umu (>=1.4.3) reads ``images/latest-public-beta.txt`` and
    fetches from the numbered directory that file names, which serves
    normally — so a wipe is now genuinely recoverable and this guard is no
    longer working around a dead endpoint.

    The single-attempt rule stays anyway, because "umu cannot install this
    runtime" has other causes (no network, full disk, a future repo change).
    It is a loop breaker, not a 403 workaround: after one failed repair we
    leave the variant alone and let :func:`unrecoverable_runtime_variants`
    turn it into a real error rather than an infinite silent retry. The TTL
    is deliberately short so a transient cause self-heals on the next launch.
    """
    for variant in UMU_RUNTIME_VARIANTS:
        variant_dir = UMU_CACHE_DIR / variant
        if not (variant_dir.is_dir() and not _runtime_entry_point_ok(variant_dir)):
            continue
        if _repair_recently_attempted(variant):
            logger.error(
                "[launcher.umu] runtime '%s' is STILL incomplete after a "
                "recent repair — umu cannot download it (check game.log for "
                "the umu error). Leaving it in place; deleting it again "
                "would only spin.", variant,
            )
            continue
        logger.warning(
            "[launcher.umu] runtime '%s' present but entry point missing "
            "— removing so umu re-downloads it", variant,
        )
        shutil.rmtree(variant_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            UMU_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _repair_marker(variant).touch()
def ensure_umu_runtime_ready() -> None:
    """Ensure UMU runtime ready."""
    # The Steam Linux Runtime (steamrt2/3/4, depending on which one the
    # selected Proton build requires) is downloaded by the first
    # ``umu-run`` and is the slowest part of a first-ever launch
    # (hundreds of MB), with no native progress — exactly the
    # "is it frozen?" gap the user hit. Toast once when it's missing so
    # the wait is expected. Fires only on the genuine first setup; the
    # cache then persists and is shared across every game.
    if not any((UMU_CACHE_DIR / variant).exists() for variant in UMU_RUNTIME_VARIANTS):
        launcher_toast(
            "toasts.launcher.downloadingRuntime",
            i18n_title_key="toasts.launcher.firstTimeSetup",
        )
    UMU_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["UMU_LOG"] = "1"
    os.environ["UMU_NO_PROTON"] = "0"
    config_dir = Path("~/.config/umu").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

def _is_recoverable(rc: int, ran_for: float) -> bool:
    """Whether ``rc`` means "the launch failed to come up", not "the game exited".

    A recoverable code (2 / 74 / 127) only carries that meaning when it
    arrives quickly. Past :data:`_RECOVERABLE_MAX_RUNTIME_SECONDS` the
    process demonstrably ran, so the code is the game's own exit status:
    retrying would relaunch a game the user just quit, and for 2/74 it
    would also wipe the shared umu runtime every other game depends on.

    Only reachable since UD-126 for Epic, which used to report
    *legendary's* exit code (always 0 — it fire-and-forgot the game)
    rather than the game's.
    """
    return (
        rc in _RECOVERABLE_CODES
        and ran_for < _RECOVERABLE_MAX_RUNTIME_SECONDS
    )


async def _prepare_retry(rc: int, attempt: int, max_attempts: int) -> None:
    """Toast, optionally wipe the runtime cache, then back off."""
    wipe = rc in _RUNTIME_CORRUPTION_CODES
    logger.warning(
        "[launcher.umu] recoverable rc=%d, retry (wipe_cache=%s)", rc, wipe,
    )
    launcher_toast(
        "toasts.launcher.retryingUmu",
        i18n_title_key="toasts.launcher.launchRetry",
        i18n_params={
            "seconds": _RETRY_BACKOFF_SECONDS,
            "attempt": attempt + 1,
            "max": max_attempts,
        },
        severity="warning",
    )
    if wipe:
        cleanup_umu_runtime_cache()
    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)


async def run_umu_with_retry(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    max_attempts: int = 2,
    on_start: Callable[[object], None] | None = None,
    should_retry: Callable[[], bool] | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 — bounds a subprocess wait via wait_for + killpg, not an asyncio.timeout() wrapper
    reap_wineserver: bool = True,
) -> int:

    """Run UMU with retry.

    ``timeout`` bounds each attempt's ``proc.wait()``. It defaults to
    ``None`` (unbounded — a real game launch runs for hours), so the
    bound is strictly opt-in per caller: only the short prefix-compat
    steps (winetricks / vcruntime regedit) pass one. On timeout the
    process group is force-killed and the attempt returns
    :data:`UMU_TIMEOUT_RC`, which is deliberately *not* recoverable, so
    a hung Proton fails the step instead of retrying into the same hang.

    ``should_retry`` is a last-word veto, consulted only once a code has
    already been judged recoverable. The recoverable test is
    code-and-duration and cannot see intent, which is a problem for any run
    the user can close by hand: sign-in windows exit inside
    :data:`_RECOVERABLE_MAX_RUNTIME_SECONDS` all the time, and reopening one
    the user just dismissed is worse than not retrying a genuine crash. A
    caller that can tell the two apart says so here. Returning False ends the
    run with the real exit code, exactly as an unrecoverable one would.

    Pass ``reap_wineserver=False`` when this run shares a prefix with a
    wineserver it does **not** own — Battle.net phase C, which sends an
    ``--exec`` to a client another run started. The reap is prefix-scoped,
    not tree-scoped: it SIGKILLs every holder of that prefix's server dir,
    so on the timeout path it killed the live client mid-download. The
    default stays ``True`` — one umu run per prefix is the norm, and the
    reap is what keeps a timed-out compat step from wedging its retry.
    """
    last_rc = 1
    game_log = open_game_log()
    out = game_log if game_log is not None else None
    try:
        for attempt in range(1, max_attempts + 1):
            logger.info(
                "[launcher.umu] run attempt %d/%d: %s (output → %s)",
                attempt, max_attempts, argv[:3],
                "game.log" if out is not None else "inherited",
            )
            started_at = _now()
            rc = await _run_umu_once(
                argv, env, cwd, out, on_start, timeout, reap_wineserver,
            )
            ran_for = _now() - started_at
            last_rc = rc
            logger.info(
                "[launcher.umu] attempt %d exit code: %d (ran %.1fs)",
                attempt, rc, ran_for,
            )
            if rc == 0:
                return 0
            if not _is_recoverable(rc, ran_for):
                return rc
            if should_retry is not None and not should_retry():
                logger.info(
                    "[launcher.umu] rc=%d is recoverable but the caller vetoed "
                    "a retry", rc,
                )
                return rc
            if attempt < max_attempts:
                await _prepare_retry(rc, attempt, max_attempts)
                continue
            return rc
        return last_rc
    finally:
        if game_log is not None:
            with contextlib.suppress(OSError):
                game_log.close()


def _strip_loader_env(env: dict[str, str] | None) -> None:
    """Drop both dynamic-loader variables from a spawn environment.

    Belt-and-suspenders: neither may reach umu-run/pressure-vessel,
    regardless of what built ``env``.

    ``LD_PRELOAD`` — re-exporting the host's gameoverlayrenderer.so crashes
    the game process with "WARNING: Keyboard Interrupt".

    ``LD_LIBRARY_PATH`` — umu copies it into ``STEAM_RUNTIME_LIBRARY_PATH``
    (``umu_run.enable_steam_game_drive``), so a *host* library path rides
    into the pressure-vessel container and shadows the container's own libs.
    The container then can't start ``python3`` — the interpreter of Proton's
    launch script — which dies with "error while loading shared libraries:
    libz.so.1" and umu exits 127.

    Where the stray value comes from is NOT established: it was observed on
    a plain Steam stable client, so the obvious guess (a containerised Steam
    exporting pressure-vessel override paths) is ruled out for that report.
    Provenance doesn't change the remedy — nothing downstream of here wants
    a host loader path.

    Epic was immune only because ``handlers/epic.py`` already wraps its
    umu-run invocation in ``env -u LD_LIBRARY_PATH -u LD_PRELOAD``;
    GOG/Amazon/Ubisoft/raw-exe reach this spawn point directly. Doing it
    here covers every store at the single choke point.
    See ``sanitize_frozen_loader_env``.
    """
    if env is None:
        return
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)


async def _reap_umu_tree(
    proc: asyncio.subprocess.Process,
    env: dict[str, str] | None,
    *,
    reap_wineserver: bool = True,
) -> None:
    """Kill umu's whole process group plus its prefix wineserver, then wait.

    ``start_new_session=True`` detaches the umu-run / pressure-vessel /
    wineserver descendants, so without this they outlive both the timeout
    and the cancellation path and keep spinning.

    The killpg is unconditional — it only ever reaches *this* run's own
    session. The wineserver reap is prefix-scoped and therefore opt-out;
    see :func:`run_umu_with_retry`.
    """
    _kill_process_group(proc)
    if reap_wineserver:
        _reap_prefix_wineserver(env)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)


async def _run_umu_once(
    argv: list[str],
    env: dict[str, str] | None,
    cwd: Path | None,
    out: Any,
    on_start: Callable[[object], None] | None,
    timeout: float | None = None,  # noqa: ASYNC109 — bounds a subprocess wait via wait_for + killpg, not an asyncio.timeout() wrapper
    reap_wineserver: bool = True,
) -> int:
    """Spawn one umu process, fire ``on_start``, await its exit code.

    When ``timeout`` is set, a process that outlives it is force-killed
    (whole process group) and :data:`UMU_TIMEOUT_RC` is returned; with
    ``timeout is None`` the wait is unbounded (the launch default).
    Cancellation (e.g. the user hitting Cancel on a "Setting up game…"
    install) also reaps the process group before propagating, so no
    orphaned wineserver is left spinning.
    """
    _strip_loader_env(env)
    assert_proton_still_complete(env)
    # If Steam wrapped our launcher in its OWN pressure-vessel (the user set
    # Properties > Compatibility on this shortcut — a supported workflow),
    # hop back out to the host first: Proton's python3 cannot load libz.so.1
    # inside steamrt and umu would exit 127. No-op when unwrapped.
    # See container_escape for the on-device reproduction.
    argv = escape_argv(argv, env, cwd)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(cwd) if cwd else None,
        stdout=out,
        stderr=out,
        start_new_session=True,
    )
    if on_start is not None:
        try:
            on_start(proc)
        except Exception:
            logger.exception("[launcher.umu] on_start callback failed")
    try:
        if timeout is None:
            return await proc.wait()
        try:
            return await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "[launcher.umu] %s exceeded %ds — killing process group",
                argv[:3], int(timeout),
            )
            await _reap_umu_tree(proc, env, reap_wineserver=reap_wineserver)
            return UMU_TIMEOUT_RC
    except asyncio.CancelledError:
        # Reap the whole tree before unwinding the cancellation.
        await _reap_umu_tree(proc, env, reap_wineserver=reap_wineserver)
        raise
