"""Phases C and D of a Battle.net launch: send the command, prove it worked.

py_modules/unifideck/launcher/proton/handlers/battlenet_exec.py

Split out of ``battlenet.py`` (2026-09-20) for its LOC cap. These two
belong together: the command is fire-and-forget by nature, so the proof
that anything happened is a separate observation of the prefix.

Two measured facts shape both halves:

* **Phase C must use ``PROTON_VERB=run``.** ``waitforexitandrun`` runs
  ``wineserver -w`` first, which blocks on the prefix's existing
  wineserver — in phase C, the client this launch just started.
* **The client accepts a command it cannot act on, silently.** An obsolete
  family code (Diablo IV's ``D4`` after the rename to ``Fen``) produces no
  error, no dialog and no exit code, so only a new game process is
  evidence that the launch landed.

``fail`` is passed in rather than imported: the caller owns what a failure
looks like to the user, and importing it back would be a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.handlers.battlenet_client import record_launch_ok
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry
from unifideck.launcher.types.errors import GameFailedError

logger = logging.getLogger(__name__)

# Bounded on purpose: this is the silent-failure detector.
GAME_APPEAR_TIMEOUT = 180.0
# The exec invocation does not exit promptly even on success, so it is
# fire-and-bounded-wait rather than awaited to completion.
EXEC_TIMEOUT = 60.0

async def issue_exec(plan: ProtonLaunchPlan, client_exe: Path, command: str) -> None:
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


async def issue_and_confirm(
    plan: ProtonLaunchPlan, client_exe: Path, uid: str, family: str,
    *, fail: Callable[..., GameFailedError], client_selects: bool = False,
) -> tuple[str | None, set[str]]:
    """Phases C + D: send the launch, then prove a game process appeared.

    Returns ``(pid, before)`` — the new pid, and the pre-launch snapshot
    phase E needs to follow a launcher-to-game hand-off. Raises when
    nothing started, because the client accepts an obsolete family code
    and does nothing — no error, no dialog, no exit code — so only a new
    process is evidence.

    Except when ``client_selects``: the client only *selects* a version
    that is not the program's retail product, and the user presses Play
    there. ``pid`` is then ``None`` and the caller waits on the client
    instead. Measured — ``launch WoWC`` sets the active product to
    ``WoW_wow_classic_era`` and stops, while ``launch W3`` goes on to
    ``LaunchBinary``. Failing that case would report a broken launch for a
    client sitting on the right page, waiting to be clicked.
    """
    before = watch.game_pids(plan.prefix_path)
    await issue_exec(plan, client_exe, f"launch {family}")

    pid = await watch.wait_for_game(plan.prefix_path, before, GAME_APPEAR_TIMEOUT)
    if pid is None and client_selects:
        logger.info(
            "[battlenet] %s is on the client's %s page — the version picker "
            "is the user's, so no game process is expected yet", uid, family,
        )
        return None, before
    if pid is None:
        raise fail(
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


