"""launcher/proton/infrastructure/companions.py — extra exes alongside a game.

Lets a game launch bring along "companion executables" — trainers, cheat
tools, or any other utility the user attaches via the "Companion
executables" picker — in the SAME Proton prefix/env as the main game,
without touching Steam's launch options or ``%command%`` at all (the
CheatDeck approach this replaces edits ``PROTON_REMOTE_DEBUG_CMD`` into
the shortcut's Launch Options, which both breaks the ``store:game_id``
argv the dispatcher depends on and requires a "skip launcher check"
workaround for stores where the game's own exe is itself a launcher).

A companion is spawned by invoking Proton's OWN ``proton`` script directly
with the ``runinprefix`` verb — deliberately NOT through ``umu-run`` (the
wrapper the main game uses). ``umu-run`` builds a brand-new
pressure-vessel/bwrap container sandbox on EVERY invocation, even when
``WINEPREFIX``/``STEAM_COMPAT_DATA_PATH`` are identical to an already-running
game's. Two separate sandboxes leave the companion's ``OpenProcess`` call
against the main game's PID denied ("Address not found. Reason: Access
Denied.", the FLiNG-trainer error observed on-device) even though both wine
clients share the same prefix, because process/ptrace visibility is scoped
per-sandbox, not per-prefix.

This mirrors ``protontricks-launch --no-bwrap`` — the community-documented,
working way to inject a trainer into an already-running Proton game — which
also calls ``proton`` directly rather than going through any container
wrapper, landing the companion in the SAME namespace as the launcher process
(and thus the same one the main game's ``umu-run`` invocation is a
descendant of), with visibility into the main game's process tree. See
``proton``'s own source (the ``runinprefix`` verb: just
``run_proc([wine_bin] + argv)``, no container, no wineserver wait) for why
this specific verb was chosen over ``run``/``waitforexitandrun`` — see
:func:`start_companions` for why ``waitforexitandrun``, the main game's own
verb, would otherwise deadlock a companion in an already-occupied prefix.

The env is the plan's ``env`` copied verbatim (same ``WINEPREFIX``,
``STEAM_COMPAT_DATA_PATH``, etc., so it lands in the exact same wine
environment) with ``PROTON_VERB`` set to ``runinprefix`` — every other var
matches the main game's, per the umu-launcher FAQ's rule for running a
second process in an already-occupied prefix. Companions are fire-and-forget:
a failure to spawn or a nonzero exit is logged and toasted, never raised, so
a broken trainer can never fail or block the actual game launch.

``CompanionSupervisor`` owns the lifecycle: :func:`start_companions`
schedules each entry (respecting its configured delay) as a background
asyncio task and returns a supervisor; the caller stops it — cancelling
any not-yet-started delay and killing every process group already
spawned — once the main game exits (see
``services.launcher.orchestrator``).
"""
from __future__ import annotations

import asyncio
import binascii
import contextlib
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.infrastructure.container_escape import (
    escape_argv,
)

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
    from unifideck.launcher.types.context import CompanionExecutable

logger = logging.getLogger(__name__)

_LAUNCHES_DIR = Path("~/.local/share/unifideck/launches").expanduser()


def _companion_fake_appid(game_key: str, companion_path: str) -> int:
    """Deterministic, unsigned 32-bit id used ONLY for gamescope focus tagging.

    NOT a real Steam shortcut appid — never written to shortcuts.vdf and
    never surfaced to Steam's library. It exists purely so the companion's
    window can be tagged with a ``STEAM_GAME`` X11 property distinct from
    the main game's real appid (see ``gamescope_window_tagger``'s module
    docstring): gamescope's focus picker treats each distinct ``STEAM_GAME``
    value as its own focusable app, but paints only ONE top-level window per
    value — tagging a companion with the SAME value as the main game made
    the trainer's window simply never get painted while the game's window
    (mapped first) held that slot, only appearing once the game's window
    unmapped on exit. A companion gets its own dedicated value instead, so
    gamescope treats it as a genuinely separate app, reachable independently
    via the Steam button's app switcher.

    Deterministic (CRC32 of ``f"{game_key}|{companion_path}"``) so the same
    companion is tagged identically across repeated launches without
    needing to persist anything.

    Constrained to the POSITIVE signed-32-bit range (``0x40000000`` ..
    ``0x7FFFFFFF``): an earlier revision forced the HIGH bit on
    (``| 0x80000000``) to keep clear of real low-valued appids, but that
    yields values above 2^31 which read as NEGATIVE when interpreted as a
    signed ``int32`` — and gamescope/steamcompmgr handles appids as signed
    ints in places (``get_prop`` returns into signed types, ``w->appID``
    comparisons). On-device that manifested as the companion's window not
    being composited AT ALL (worse than merely being hidden behind the
    game), which is exactly what happened when this fake appid was first
    tried. Forcing bit 30 instead keeps the value comfortably positive
    while still landing far above any real Steam appid (>1 billion), so it
    can't collide with the main game's own.
    """
    key = f"{game_key}|{companion_path}"
    return (binascii.crc32(key.encode("utf-8")) & 0x3FFFFFFF) | 0x40000000


def _open_companion_log(companion_path: str) -> Any:
    """Open a per-companion stdout+stderr log file for this launch.

    Companions were previously spawned with no stdout/stderr capture at
    all — inheriting whatever the parent had, which for a launch started
    from Steam is nothing usable — so a trainer that crashed, showed a
    ".NET/VC++ redistributable missing" dialog, or simply never created a
    window left ZERO trace anywhere, making "the trainer doesn't appear"
    reports undiagnosable (see the main game's identical rationale in
    ``umu_runtime.open_game_log``). Named
    ``<launch_id>.companion.<basename>.log`` so multiple companions in one
    launch don't collide. Returns ``None`` on any error, in which case the
    caller falls back to inheriting the parent's stdout/stderr.
    """
    from unifideck.launcher.diagnostics.correlation import get_launch_id
    try:
        _LAUNCHES_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path(companion_path).name or "companion"
        path = (
            _LAUNCHES_DIR
            / f"{get_launch_id()}.companion.{safe_name}.log"
        )
        return path.open("a", encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("[launcher.companions] companion log open failed: %s", e)
        return None

# A companion spawned at the same instant as the main game races it inside
# the same fresh Wine prefix — two umu-run/wineserver processes touching the
# same WINEPREFIX registry/lock files simultaneously. Observed on-device: a
# trainer with 0s delay made the MAIN game exit with umu rc=41 within ~9s,
# while the identical launch with the companion removed (or delayed) started
# fine. Enforcing a floor here — rather than only documenting "add a delay"
# — means a companion configured with 0s (the picker's own default) can
# never trigger this by surprise; it costs nothing when the prefix is
# already warm (a longer-running game's second-plus launch), and only adds
# a few seconds on a fresh prefix's very first launch.
_MIN_COMPANION_DELAY_SECONDS = 10.0


def _kill_process_group(pid: int) -> None:
    """Best-effort SIGKILL of the process group rooted at ``pid``.

    Local copy of the same pattern used by ``umu_runtime._kill_process_group``
    — companions are spawned with ``start_new_session=True`` too, so killing
    just the pid would leave any pressure-vessel/wineserver descendants
    running.
    """
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as e:
        logger.warning("[launcher.companions] failed to kill process group: %s", e)


@dataclass
class CompanionSupervisor:
    """Tracks every companion task/process spawned for one launch.

    ``tasks`` holds the asyncio tasks running :func:`_run_one_companion`
    (covers both the "still waiting on its delay" and "process is running"
    states); ``stop_all`` cancels/kills all of them uniformly rather than
    needing to distinguish the two.
    """
    tasks: list[asyncio.Future[None]] = field(default_factory=list)

    async def stop_all(self) -> None:
        """Cancel pending delays and kill every spawned companion process.

        Called once the main game exits. Cancelling a task whose delay
        hasn't elapsed yet simply prevents that companion from ever
        starting; a task already running its process gets a SIGKILL via
        the ``finally`` in :func:`_run_one_companion`, triggered by the
        ``CancelledError``.
        """
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _register_companion_with_tagger(
    proc_pid: int, companion_path: str, fake_appid: int,
) -> None:
    """Register a companion's exe name + PID with the gamescope tagger.

    Both map to the companion's own dedicated ``fake_appid`` so
    ``gamescope_window_tagger`` can retag its window (via the X11
    ``STEAM_GAME`` property) as a SEPARATE focusable app. The companion
    inherits the main game's ``SteamAppId``, so Wine gives its window the
    same ``steam_app_<main game>`` WM_CLASS — WM_CLASS is therefore useless
    for telling them apart.

    The EXE NAME is the rule that actually matches; the PID is only a
    fallback. Registering the PID alone was the reason this never worked:
    ``proc_pid`` is the ``proton runinprefix`` process, but Wine reparents
    the process that owns the trainer's window out of that subtree (measured
    on-device: window PID 7098 had PPID 1398 while ``proc.pid`` was 7095),
    so the tagger's ancestry walk never found a match and the window fell
    through to the WM_CLASS rule — getting the MAIN GAME's appid, the very
    thing the fake appid exists to avoid.

    Non-fatal: both windows exist either way (visible side by side in
    Desktop Mode); this only affects whether gamescope in Gaming Mode treats
    them as one app or two independently selectable ones.
    """
    try:
        from unifideck.launcher.proton.infrastructure.gamescope_window_tagger import (
            register_companion_exe,
            register_companion_pid,
        )
        register_companion_exe(Path(companion_path).name, fake_appid)
        register_companion_pid(proc_pid, fake_appid)
    except Exception:
        logger.debug(
            "[launcher.companions] gamescope companion registration "
            "failed (non-fatal)", exc_info=True,
        )


async def _spawn_companion(
    companion_path: str,
    *,
    proton_script: Path,
    env: dict[str, str],
    cwd: Path | None,
    log_file: Any,
) -> asyncio.subprocess.Process:
    """Spawn one companion via ``proton runinprefix``, container-escaped.

    Calls proton's OWN script directly (bypassing umu-run) with the verb umu
    passes as ``sys.argv[1]`` — see this module's docstring: umu-run would
    build its own container sandbox here, which is exactly what breaks the
    companion's process visibility into the already-running main game.

    Escapes Steam's pressure-vessel container first, like every other
    direct-spawn site in this launcher (prefix_init, gog_setup,
    epic_prerequisites, battlenet) — see ``infrastructure.container_escape``
    for the on-device libz.so.1/rc=127 failure this guards against. No-op
    when this process isn't containerised (the common case).
    """
    argv = escape_argv(
        [str(proton_script), "runinprefix", companion_path], env, cwd,
    )
    logger.info(
        "[launcher.companions] starting companion: %s (output → %s)",
        companion_path, "companion log" if log_file else "inherited",
    )
    return await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(cwd) if cwd else None,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )


async def _run_one_companion(
    companion_path: str,
    delay_seconds: float,
    *,
    proton_script: Path,
    env: dict[str, str],
    cwd: Path | None,
    game_title: str,
    fake_appid: int,
) -> None:
    """Wait ``delay_seconds``, then spawn+await one companion executable.

    Runs as a background task (see :func:`start_companions`) — never
    awaited by the main launch path, so nothing here can block or fail
    the game. Any error (missing file, spawn failure, nonzero exit) is
    logged and best-effort toasted, not raised. On cancellation (the main
    game exiting — see ``CompanionSupervisor.stop_all``) the spawned
    process's whole group is killed before the ``CancelledError``
    propagates, so a still-open trainer never outlives its game.
    """
    proc: asyncio.subprocess.Process | None = None
    effective_delay = max(delay_seconds, _MIN_COMPANION_DELAY_SECONDS)
    log_file = _open_companion_log(companion_path)
    try:
        if effective_delay > 0:
            await asyncio.sleep(effective_delay)
        if not await asyncio.to_thread(Path(companion_path).is_file):
            logger.warning(
                "[launcher.companions] companion executable missing, "
                "skipping: %s", companion_path,
            )
            return
        proc = await _spawn_companion(
            companion_path,
            proton_script=proton_script, env=env, cwd=cwd, log_file=log_file,
        )
        _register_companion_with_tagger(
            proc.pid, companion_path, fake_appid,
        )
        rc = await proc.wait()
        if rc != 0:
            logger.warning(
                "[launcher.companions] companion %s exited %d",
                companion_path, rc,
            )
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            _kill_process_group(proc.pid)
        raise
    except Exception:
        logger.exception(
            "[launcher.companions] companion %s failed to start",
            companion_path,
        )
        try:
            launcher_toast(
                "toasts.launcher.companionStartFailed",
                i18n_title_key="toasts.launcher.launchWarning",
                game_title=game_title,
                severity="warning",
            )
        except Exception:  # a toast must never break companion handling
            logger.debug("[launcher.companions] toast failed", exc_info=True)
    finally:
        if log_file is not None:
            with contextlib.suppress(OSError):
                log_file.close()


def _schedule_companion(
    companion: CompanionExecutable,
    plan: ProtonLaunchPlan,
    proton_script: Path,
    cwd: Path | None,
) -> asyncio.Future[None]:
    """Build one background task for a single companion executable.

    The companion inherits the main game's ``env`` verbatim (``dict(plan.env)``)
    with ONE change: ``PROTON_VERB=runinprefix``. umu-run's default verb
    ``waitforexitandrun`` blocks the new proton invocation on ``wineserver
    -w`` until every OTHER wine process in the prefix exits — correct for the
    main game, but a companion launched into a prefix the game is ALREADY
    using would hang forever (observed on-device: the companion's
    ``proton``/``wineserver -w`` sitting alive for minutes with no trainer
    spawned, while the game ran unaffected). ``runinprefix`` is the documented
    umu-launcher fix for running more than one program in one prefix; every
    OTHER env var must stay identical for wine/proton internal state to agree.

    ``SteamAppId``/``SteamGameId`` are left INHERITED, not overridden or
    popped: both were tried to give the companion its own gamescope focus
    slot and both regressed to NO window rendered at all. This matches
    CheatDeck's approach (plain env inheritance, manual Steam-button window
    switching), which is simpler and more reliable than a from-scratch
    per-companion focus slot.
    """
    fake_appid = _companion_fake_appid(plan.context.game_key, companion.path)
    companion_env = dict(plan.env)
    companion_env["PROTON_VERB"] = "runinprefix"
    return asyncio.ensure_future(
        _run_one_companion(
            companion.path,
            companion.delay_seconds,
            proton_script=proton_script,
            env=companion_env,
            cwd=cwd,
            game_title=plan.context.game_key,
            fake_appid=fake_appid,
        ),
    )


def start_companions(plan: ProtonLaunchPlan) -> CompanionSupervisor:
    """Schedule every configured companion executable for this launch.

    Returns immediately — nothing here is awaited by the caller. Each
    companion gets its own background task (delay honoured independently),
    invoking proton's own script directly (see module docstring for why —
    NOT the ``umu_wrapper``/``python_bin`` the main game uses) with the
    SAME ``env`` the main game uses, so it runs in the identical Proton
    prefix. Returns an empty ``CompanionSupervisor`` when the game has none
    configured, or when ``proton_path`` was never resolved (should not
    happen by the time a plan exists, but fails soft rather than raising
    into the main launch).
    """
    supervisor = CompanionSupervisor()
    companions = plan.context.companion_executables
    if not companions:
        return supervisor
    proton_script = plan.state.proton_path
    if proton_script is None:
        logger.warning(
            "[launcher.companions] no proton_path on state, skipping %d "
            "companion executable(s) for %s",
            len(companions), plan.context.game_key,
        )
        return supervisor
    cwd = (
        plan.context.exe_path.parent
        if plan.context.exe_path.parent.is_dir()
        else None
    )
    for companion in companions:
        supervisor.tasks.append(
            _schedule_companion(companion, plan, proton_script, cwd),
        )
    logger.info(
        "[launcher.companions] scheduled %d companion executable(s) for %s",
        len(companions), plan.context.game_key,
    )
    return supervisor
