"""Battle.net launch handler — two-phase, with post-launch verification.

py_modules/unifideck/launcher/proton/handlers/battlenet.py

Launching a Battle.net game is not one command. The client must already be
running before it will accept a launch instruction, so::

    Phase A  start Battle.net Launcher.exe, DETACHED
             PROTON_VERB=waitforexitandrun — this run owns the wineserver
    Phase B  poll until a CEF renderer exists in THIS prefix
    Phase C  Battle.net.exe --exec="launch <FAMILY>"
             PROTON_VERB=run  <-- load-bearing, see below
    Phase D  verify a game process actually appeared
    Phase E  watch until it exits

**Phase C must use ``PROTON_VERB=run``.** ``waitforexitandrun`` runs
``wineserver -w`` first, which blocks until the prefix's existing wineserver
exits — and in phase C that wineserver is the client we just started.
Measured on-device: with ``waitforexitandrun`` the second invocation never
reaches the exe at all and the command never lands; with ``run`` it works.

**Phase D is mandatory, not defensive.** Blizzard renamed Diablo IV's family
code ``D4`` -> ``Fen`` in 2026, and the client *accepts the obsolete code and
does nothing* — no error, no dialog, no exit code. The only way to know a
launch worked is to see a new game process. For the same reason the phase C
return code is ignored: the client forwards the command and exits, so its rc
says nothing about the game.

Only one argument is passed. NonSteamLaunchers issue #957 reports a shortcut
opening the launcher instead of the game while passing both ``--exec`` and a
``battlenet://`` URI; the conflicting second argument is a prime suspect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from unifideck.launcher import wrapper_session
from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.game_title import resolve_title
from unifideck.launcher.proton.handlers import battlenet_auth_wsi as auth_wsi
from unifideck.launcher.proton.handlers import battlenet_bootstrap as bootstrap
from unifideck.launcher.proton.handlers import battlenet_login_state as login_state
from unifideck.launcher.proton.handlers import battlenet_session as session
from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.handlers import battlenet_wsi, wrapper_clients
from unifideck.launcher.proton.handlers.battlenet_client import (
    find_client_exe,
    find_launcher_exe,
    record_launch_ok,
    resolve_family,
)
from unifideck.launcher.proton.infrastructure.container_escape import (
    escape_argv,
)
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.game_log import (
    open_game_log,
)
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry
from unifideck.launcher.types.errors import GameFailedError

logger = logging.getLogger(__name__)

# Must outlast a cold start plus a forced client self-update — the client
# updated itself within five minutes of first launch during the spike.
CLIENT_READY_TIMEOUT = 300.0
# Bounded on purpose: this is the silent-failure detector.
GAME_APPEAR_TIMEOUT = 180.0
# The exec invocation does not exit promptly even on success, so it is
# fire-and-bounded-wait rather than awaited to completion.
EXEC_TIMEOUT = 60.0
# How long a client-less Wine session gets to produce a renderer before we
# call it stale. Short: a healthy client never reaches this path.
STALE_SESSION_GRACE = 20.0
# How long the client gets to finish logging in once it is up. Generous
# because a client that comes up signed *out* puts the login page on screen,
# and this is the window in which the user can answer it. Measured: a healthy
# cold start signs in ~10s after the renderer appears.
LOGIN_SETTLE_TIMEOUT = 180.0

STORE = "battlenet"



def _fail(
    plan: ProtonLaunchPlan,
    key: str,
    message: str,
    *,
    rc: int = 1,
    titled: bool = True,
    **context: object,
) -> GameFailedError:
    """Toast the failure and build the error to raise.

    ``titled=False`` for messages with no ``{{gameTitle}}`` placeholder.
    The auth shortcut is not a game: ``resolve_title`` finds no registry row
    for ``battlenet:bnet-auth`` and returns the key itself, which is how a
    user came to read "isn't set up for battlenet:bnet-auth yet".
    """
    launcher_toast(
        f"toasts.launcher.{key}Message",
        i18n_title_key=f"toasts.launcher.{key}",
        game_title=resolve_title(plan.context.game_key) if titled else "",
        severity="error",
    )
    plan.state.game_exit_code = rc
    return GameFailedError(message, subprocess_rc=rc, context=dict(context))


async def _start_client_detached(
    plan: ProtonLaunchPlan, launcher_exe: Path,
) -> asyncio.subprocess.Process:
    """Phase A. Owns the wineserver session, so it keeps waitforexitandrun.

    Output goes to the per-launch ``game.log``, the same place
    ``run_umu_with_retry`` sends every other launch path. It used to go to
    ``DEVNULL``, and the cost was measured in the field: a tester's client
    started, died within ~20 s and left *nothing at all* to read — no umu
    banner, no Wine error, no exit code — so a five-minute failure had to be
    reasoned about from the surrounding logs instead of read off disk.

    The process handle is returned rather than discarded so the readiness
    wait can notice the client exiting. Detached (``start_new_session``) is
    unchanged: this run owns the wineserver and must outlive us.
    """
    # Escape Steam's pressure-vessel when Force-Compat wrapped us, or the
    # client nests a second container and never starts. No-op when
    # unwrapped. See infrastructure.container_escape.
    argv = escape_argv(
        [str(plan.python_bin), str(plan.umu_wrapper), str(launcher_exe)],
        plan.env, None,
    )
    logger.info("[battlenet] phase A: starting client")
    game_log = open_game_log()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=plan.env,
            stdout=game_log if game_log is not None else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT if game_log is not None
            else asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # Ours to close: the child holds its own duplicated descriptor, so
        # closing here does not truncate its output. Leaving it open would
        # leak a handle for the life of the launcher.
        if game_log is not None:
            with contextlib.suppress(OSError):
                game_log.close()
    if plan.on_process_start:
        with contextlib.suppress(Exception):
            plan.on_process_start(proc)
    return proc


async def _issue_exec(plan: ProtonLaunchPlan, client_exe: Path, command: str) -> None:
    """Phase C. PROTON_VERB=run, one argument, return code ignored.

    ``reap_wineserver=False`` is load-bearing. This run shares its prefix
    with the client phase A started and does not own that wineserver, so
    the :data:`EXEC_TIMEOUT` cancellation must reap only its own process
    group. It did not: the prefix-scoped reap SIGKILLed the live client
    60 s into every launch, killing the Battle.net Agent mid-download.
    Measured on-device — every Diablo II install stalled inside a minute,
    frozen at 27%, with the Agent's log going silent at the reap's exact
    timestamp. See ``infrastructure/wineserver_reap`` for the scope rule.
    """
    env = dict(plan.env)
    env["PROTON_VERB"] = "run"
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(client_exe), f"--exec={command}"]
    logger.info("[battlenet] phase C: --exec=%s (PROTON_VERB=run)", command)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(
            run_umu_with_retry(argv, env=env, max_attempts=1, reap_wineserver=False),
            timeout=EXEC_TIMEOUT,
        )


async def _clear_stale_session(plan: ProtonLaunchPlan) -> None:
    """Clear a Wine session that has no usable client left.

    Phase A runs ``waitforexitandrun``, which blocks on the prefix's
    existing wineserver — so a dead session does not just sit there, it
    wedges the next launch. Sessions were being left behind because
    teardown only recognised the client's main process, so once that died
    its CEF children and Wine infrastructure survived unsignalled and the
    next launch stacked a second client on top. Two were measured on-device.

    Costs nothing on a cold prefix (no Wine processes) or a healthy one
    (the caller's readiness check returns first). The grace period is paid
    only when a session exists but is not becoming ready — which is also
    what distinguishes "stale" from "still starting up".
    """
    if not watch.wine_pids(plan.prefix_path):
        return
    if await watch.wait_for_client_ready(plan.prefix_path, STALE_SESSION_GRACE):
        return
    logger.warning("[battlenet] stale Wine session in %s — clearing", plan.prefix_path)
    await asyncio.to_thread(watch.stop_stale_session, plan.prefix_path)


async def _release_other_clients(plan: ProtonLaunchPlan) -> None:
    """Make sure no client is running in a *different* prefix.

    Every prefix is a clone, so every client presents the same
    ``Client.GaClientId`` and the same token. Two running at once both refresh
    that token and the server invalidates one of them — which is how the user
    reaches "Your login session has expired" by opening the Sign-In tile and
    then launching a game.

    The auth prefix's client is closed: it exists only to sign in, never
    downloads anything, and it writes its session directly into the prefix we
    are about to inject from, so stopping it is both safe and necessary.

    A client in any *other* game prefix is left strictly alone and the launch
    fails instead. It may be mid-download, and killing the Agent is a measured
    failure — every Diablo II install stalled at 27% when a reap reached it.
    """
    others = await asyncio.to_thread(
        wrapper_clients.live_client_prefixes, STORE,
        exclude=(plan.prefix_path,),
    )
    if not others:
        return
    auth = wrapper_session.auth_prefix(STORE)
    auth_resolved = auth.resolve() if auth is not None else None
    for other in others:
        if auth_resolved is not None and other == auth_resolved:
            logger.info(
                "[battlenet] closing the sign-in client in %s before starting "
                "one here (two clients share an identity and race)", other,
            )
            await asyncio.to_thread(watch.stop_client, other)
            continue
        raise _fail(
            plan,
            "battlenetClientBusyElsewhere",
            f"The Battle.net client is already running for another game "
            f"in {other.name}",
            other_prefix=str(other),
        )


async def _bring_up_client(plan: ProtonLaunchPlan) -> Path:
    """Phases A + B. Returns the client exe once it will accept commands."""
    await bootstrap.ensure_client(plan, "battlenetPrefixNotReady", fail=_fail)
    launcher_exe = find_launcher_exe(plan.prefix_path)
    client_exe = find_client_exe(plan.prefix_path)
    # ``ensure_client`` proved both above; this satisfies the type checker
    # and would only fire if the prefix vanished between the two calls.
    if launcher_exe is None or client_exe is None:  # pragma: no cover
        raise _fail(
            plan,
            "battlenetPrefixNotReady",
            "Battle.net client is not installed in this prefix",
            rc=127,
            prefix=str(plan.prefix_path),
        )

    if not watch.client_ready(plan.prefix_path):
        await _start_client_here(plan, launcher_exe)
    return client_exe


async def _start_client_here(plan: ProtonLaunchPlan, launcher_exe: Path) -> None:
    """Phase A + B proper: clear the way, start the client, wait for readiness.

    Split from :func:`_bring_up_client` to stay under the fan-out gate; the
    ordering below is load-bearing and unchanged.
    """
    # Order matters. No other client may be live when we inject — it would be
    # writing the very files we are replacing — and the injection has to land
    # before this client reads them at startup.
    await _release_other_clients(plan)
    await _clear_stale_session(plan)
    await session.inject_into(plan.prefix_path)
    # After the injection, never before. The injection carries the user's
    # launcher settings in, and it takes the *newer* settings file of the two;
    # writing the tweaks first would make this prefix's file the newer one and
    # the settings would stay behind in the prefix they were changed in.
    await bootstrap.ensure_tweaks(plan)
    if await _try_start(plan, launcher_exe):
        return
    # One retry, and only for a crash we can name. See battlenet_wsi.
    if not await battlenet_wsi.adopt_workaround(
        plan, resolve_title(plan.context.game_key),
    ):
        raise _fail(
            plan,
            "battlenetClientNotReady",
            "Battle.net client did not become ready",
            timeout=CLIENT_READY_TIMEOUT,
        )
    await _clear_stale_session(plan)
    if not await _try_start(plan, launcher_exe):
        raise _fail(
            plan,
            "battlenetClientNotReady",
            "Battle.net client did not become ready, with and without the "
            "gamescope WSI layer",
            timeout=CLIENT_READY_TIMEOUT,
        )


async def _try_start(plan: ProtonLaunchPlan, launcher_exe: Path) -> bool:
    """Start the client and wait for it to be able to accept commands."""
    proc = await _start_client_detached(plan, launcher_exe)
    launcher_toast(
        "toasts.launcher.battlenetStartingClientMessage",
        i18n_title_key="toasts.launcher.battlenetStartingClient",
        game_title=resolve_title(plan.context.game_key),
    )
    return await watch.wait_for_client_ready(
        plan.prefix_path, CLIENT_READY_TIMEOUT, proc=proc,
    )


async def _require_signed_in(plan: ProtonLaunchPlan) -> None:
    """Refuse to drive a client that is provably sitting on the login page.

    ``client_ready`` only proves a renderer exists, which is equally true of
    the login page. Sending ``--exec`` to a signed-out client is accepted and
    does nothing, so the launch died 180 s later as "no game process
    appeared" — a message that blames a family-code rename for a session
    problem, and sent at least one user hunting the wrong bug.

    Only a *proven* signed-out client fails here. ``UNKNOWN`` proceeds
    exactly as before: this is a better diagnosis, not a new gate.
    """
    state = await login_state.wait_for_login(plan.prefix_path, LOGIN_SETTLE_TIMEOUT)
    if state is not login_state.LoginState.SIGNED_OUT:
        return
    raise _fail(
        plan,
        "battlenetNotSignedIn",
        "The Battle.net client is not signed in",
        prefix=str(plan.prefix_path),
    )


async def battlenet_launch(plan: ProtonLaunchPlan) -> int:
    """Launch an installed Battle.net game through the resident client."""
    uid = plan.context.game_id
    family = resolve_family(uid)
    if not family:
        # Never fall back to "open the client bare". Battle.net's failure is
        # silent, so the user would see the client open and nothing happen.
        raise _fail(
            plan,
            "battlenetFamilyMissing",
            f"No Battle.net family code known for {uid}",
            uid=uid,
        )

    launcher_toast(
        "toasts.launcher.startingBattlenetGame",
        i18n_title_key="toasts.launcher.launchingGame",
        game_title=resolve_title(plan.context.game_key),
    )
    client_exe = await _bring_up_client(plan)
    await _require_signed_in(plan)
    pid, before = await _issue_and_confirm(plan, client_exe, uid, family)

    async with _client_teardown(plan):
        await watch.wait_for_exit(plan.prefix_path, pid, before=before)
    plan.state.game_exit_code = 0
    return 0


@contextlib.asynccontextmanager
async def _client_teardown(plan: ProtonLaunchPlan) -> AsyncGenerator[None]:
    """Stop the client in this prefix when the run ends, however it ends.

    The client is started detached (``start_new_session=True``), so it
    outlives us by default: on a normal exit Steam marks the shortcut
    stopped while Battle.net is still running, and on a stop from the UI
    the SIGTERM reaches only this launcher. Either way the user is left
    with a window whose "X" no longer talks to anything and a play session
    that never closes.

    Runs on cancellation too, which is the path the Steam stop button and
    the QAM "X" actually take.

    The session capture follows the stop, in that order, because the client
    only writes its rotated token as it shuts down.
    """
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(watch.stop_client, plan.prefix_path)
        await session.capture_from(plan.prefix_path)


async def _issue_and_confirm(
    plan: ProtonLaunchPlan, client_exe: Path, uid: str, family: str,
) -> tuple[str, set[str]]:
    """Phases C + D: send the launch, then prove a game process appeared.

    Returns ``(pid, before)`` — the new pid, and the pre-launch snapshot
    phase E needs to follow a launcher-to-game hand-off. Raises when
    nothing started, because the client accepts an obsolete family code
    and does nothing — no error, no dialog, no exit code — so only a new
    process is evidence.
    """
    before = watch.game_pids(plan.prefix_path)
    await _issue_exec(plan, client_exe, f"launch {family}")

    pid = await watch.wait_for_game(plan.prefix_path, before, GAME_APPEAR_TIMEOUT)
    if pid is None:
        raise _fail(
            plan,
            "battlenetLaunchNotObserved",
            f"Battle.net accepted 'launch {family}' but no game process appeared",
            uid=uid,
            family=family,
        )

    # This family is now proven for this uid. Record it before the
    # (potentially hours-long) exit wait: a crash or a forced shutdown
    # mid-session must not cost us the one fact that makes a later family
    # rename detectable.
    with contextlib.suppress(Exception):
        record_launch_ok(uid, family, time.time())
    return pid, before


async def battlenet_auth_launch(plan: ProtonLaunchPlan) -> int:
    """Open the client so the user can sign in.

    Blocks until the user closes it, which is what stops the Steam shortcut
    exiting immediately and tearing the window down with it.

    Installs the client first when the prefix has none. That is the normal
    path after a fresh install or a full cleanup, not an edge case.

    It also *completes* a half-installed one, and this is the prefix where
    that matters most: the template is derived from here and every game
    prefix is cloned from the template, so an interrupted install here
    poisons the whole lineage. Signing in again is the one action a user
    naturally retries, so it is the right place to heal from.
    """
    await bootstrap.ensure_client(
        plan, "battlenetAuthPrefixNotReady", fail=_fail, titled=False,
    )
    launcher_exe = find_launcher_exe(plan.prefix_path)
    if launcher_exe is None:  # pragma: no cover - ensure_client proved it
        raise _fail(
            plan,
            "battlenetAuthPrefixNotReady",
            "Battle.net client is not installed in the auth prefix",
            rc=127,
            titled=False,
            prefix=str(plan.prefix_path),
        )
    wrapper_clients.announce_client_open("battlenet")
    # The same clearing every other path does before starting a client, and
    # for a sharper reason here: this run uses the default
    # ``PROTON_VERB=waitforexitandrun``, whose ``wineserver -w`` blocks on the
    # prefix's existing wineserver. A previous sign-in that left ``Agent.exe``
    # behind — ``stop_client`` spares it deliberately, see its docstring —
    # therefore wedges the *next* sign-in with no window and no error. Both
    # calls are no-ops on a cold or healthy prefix.
    await _release_other_clients(plan)
    await _clear_stale_session(plan)
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(launcher_exe)]
    # The readiness latch is what stops this reopening a window the user just
    # closed. This run takes the default ``max_attempts=2``, and the recoverable
    # test is rc-and-duration only: closing the sign-in window inside
    # ``_RECOVERABLE_MAX_RUNTIME_SECONDS`` (120) with rc 2, 74 or 127 looked
    # exactly like the ANGLE/gamescope startup abort the retry exists for, so
    # the client came back by itself. It reported as "the sign-in launcher
    # reopens when I close it", and for rc 2 and 74 it also wiped the shared
    # umu runtime cache on the way through. Whether the renderer was ever seen
    # separates the two: a crash during renderer init never reaches it.
    #
    # The same latch drives the WSI measurement in ``auth_wsi``, which is the
    # sign-in half of what ``_start_client_here`` has always done for the
    # client. It cannot be folded into ``run_umu_with_retry``: the abort it
    # recovers from returns rc 0.
    rc = await auth_wsi.run_auth_client(
        plan, argv, teardown=_client_teardown, clear_stale=_clear_stale_session,
    )
    plan.state.game_exit_code = rc
    return rc


async def battlenet_install_launch(plan: ProtonLaunchPlan) -> int:
    """Open the client on a game's page so the user can press Install.

    ``--exec="install <FAMILY>"`` does **not** start a download — measured
    against the current client with a known-good family code. So this
    navigates and hands over, exactly as the Ubisoft install flow does; the
    download worker owns completion by polling ``product.db``.
    """
    uid = plan.context.game_id
    family = resolve_family(uid)
    if not family:
        raise _fail(
            plan,
            "battlenetFamilyMissing",
            f"No Battle.net family code known for {uid}",
            uid=uid,
        )
    launcher_toast(
        "toasts.launcher.installingBattlenetMessage",
        i18n_title_key="toasts.launcher.installingBattlenet",
        game_title=resolve_title(plan.context.game_key),
    )
    client_exe = await _bring_up_client(plan)
    # Same gate as a launch: a signed-out client accepts the navigation and
    # shows the user nothing they can install from.
    await _require_signed_in(plan)
    # Navigate to the game's page; the user presses Install there.
    await _issue_exec(plan, client_exe, f"launch {family}")
    # Stay alive while the client is up so Steam keeps the shortcut running
    # and the install window is not torn down under the user.
    async with _client_teardown(plan):
        await watch.wait_while_client_running(plan.prefix_path)
    plan.state.game_exit_code = 0
    return 0
