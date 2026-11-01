"""Start a non-retail Battle.net version by pressing the client's own Play.

py_modules/unifideck/launcher/proton/handlers/battlenet_play.py

Measured on-device (2026-11-01), every other route fails for a version that
is not the program's retail product, e.g. World of Warcraft Classic Era:

* ``--exec="launch WoWC"`` and ``--exec="launch_uid wow_classic_era"`` only
  SELECT it: the client logs ``Active product set to: WoW_wow_classic_era``
  and waits for a click. Only retail is auto-started.
* Running ``WowClassic.exe -launcherlogin -uid …`` ourselves starts a process
  that never logs in: the client logs ``Pre-existing game session detected
  without a pending launch`` and the game quits about 20 s later.

What works is what the user would do: select the exact version with
``launch_uid``, then press Play in the client's UI (over DevTools, see
``battlenet_cdp``). The client logs ``LaunchBinary: uid=…``, starts the game
itself, and the game logs in (``AccountData_Initialize`` in its own log).

``fail`` and ``restart`` come from the caller: what a failure looks like to
the user, and how to bring the client back up with DevTools enabled, are
``battlenet.py``'s, and importing them back would be a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from unifideck.launcher.proton.handlers import battlenet_cdp as cdp
from unifideck.launcher.proton.handlers import battlenet_exec as bnet_exec
from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c
from unifideck.launcher.types.errors import GameFailedError

logger = logging.getLogger(__name__)

_LOG_DIR = "users/steamuser/AppData/Local/Battle.net/Logs"
_MAX_LOG_BYTES = 8 * 1024 * 1024
#: The client UI renders its Play button some seconds after sign-in.
UI_READY_TIMEOUT = 120.0
#: ``--exec`` goes through a fresh Proton container, which takes over 20 s.
SELECT_TIMEOUT = 60.0
CLICK_TIMEOUT = 30.0
LAUNCH_TIMEOUT = 60.0
_POLL = 1.0

PLAY_BUTTON_JS = "!!document.querySelector('.play-btn.play-action')"
# The button exists before its label renders: innerText reads "" while the
# client window is not drawn (measured behind Big Picture), so the label is
# no readiness signal. ``disabled`` is.
CLICK_PLAY_JS = """(() => {
  const b = document.querySelector('.play-btn.play-action');
  if (!b || b.disabled) return false;
  b.click();
  return true;
})()"""
SHOWN_JS = """[...document.querySelectorAll('.play-selector-button')]
  .map(e => e.innerText.trim()).join(' / ')"""

Fail = Callable[..., GameFailedError]
Restart = Callable[[], Awaitable[None]]


def count_in_logs(prefix: Path, needle: str) -> int:
    """How many lines of the client's logs contain ``needle``.

    Counted rather than searched, because one client session logs many
    launches: a ``LaunchBinary`` from an earlier Play would otherwise read as
    proof of this one. Callers compare against a count taken beforehand.
    """
    drive_c = resolve_drive_c(Path(prefix))
    logs = drive_c / _LOG_DIR if drive_c else None
    if logs is None or not logs.is_dir():
        return 0
    total = 0
    for path in logs.glob("battle.net-*.log"):
        try:
            with path.open("rb") as handle:
                text = handle.read(_MAX_LOG_BYTES).decode("utf-8", "replace")
        except OSError:
            continue
        total += text.count(needle)
    return total


async def _poll(check: Callable[[], object], limit_s: float) -> bool:
    """Poll ``check`` (run off the loop) until truthy or ``limit_s`` passes."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        with contextlib.suppress(cdp.CdpError, OSError):
            if await asyncio.to_thread(check):
                return True
        await asyncio.sleep(_POLL)
    return False


async def _ui_port(plan: ProtonLaunchPlan, restart: Restart) -> int:
    """The DevTools port of the running client, restarting it once if needed.

    A client started before DevTools was enabled (or by the user) has no
    port. It is stopped and started again through the normal bring-up, which
    now passes ``--remote-debugging-port``.
    """
    port = cdp.port_for(plan.prefix_path)
    if not await asyncio.to_thread(cdp.alive, port):
        logger.info("[battlenet] client has no DevTools port; restarting it with one")
        await asyncio.to_thread(watch.stop_client, plan.prefix_path)
        await restart()
        port = cdp.port_for(plan.prefix_path)
    if port is None:
        raise cdp.CdpError("no DevTools port recorded after the client restart")
    return port


async def _select(plan: ProtonLaunchPlan, client_exe: Path, uid: str) -> bool:
    """``launch_uid <uid>``, confirmed by the client's own log line."""
    needle = f"Selecting game by uid. uid={uid} "
    before = await asyncio.to_thread(count_in_logs, plan.prefix_path, needle)
    sender = asyncio.create_task(bnet_exec.issue_exec(plan, client_exe, f"launch_uid {uid}"))
    try:
        return await _poll(
            lambda: count_in_logs(plan.prefix_path, needle) > before, SELECT_TIMEOUT,
        )
    finally:
        # Delivered or not, the hand-off has done all it will. Cancelling
        # reaps only its own process group (``reap_wineserver=False``).
        await asyncio.sleep(2)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender


async def press_play(
    plan: ProtonLaunchPlan, client_exe: Path, uid: str,
    *, fail: Fail, restart: Restart,
) -> tuple[str, set[str]]:
    """Select ``uid``, press Play, and return the game's ``(pid, before)``."""
    try:
        port = await _ui_port(plan, restart)
    except cdp.CdpError as exc:
        raise fail(plan, "battlenetLaunchNotObserved", f"Battle.net UI unreachable: {exc}",
                   uid=uid) from exc
    if not await _poll(lambda: cdp.evaluate(port, PLAY_BUTTON_JS), UI_READY_TIMEOUT):
        raise fail(plan, "battlenetLaunchNotObserved",
                   "Battle.net never showed its Play button", uid=uid)
    if not await _select(plan, client_exe, uid):
        raise fail(plan, "battlenetLaunchNotObserved",
                   f"Battle.net did not select {uid}", uid=uid)
    with contextlib.suppress(cdp.CdpError):
        shown = await asyncio.to_thread(cdp.evaluate, port, SHOWN_JS)
        logger.info("[battlenet] client selected %s (shows: %s)", uid, shown)

    launched = f"LaunchBinary: uid={uid} "
    launches_before = await asyncio.to_thread(count_in_logs, plan.prefix_path, launched)
    before = watch.game_pids(plan.prefix_path)
    if not await _poll(lambda: cdp.evaluate(port, CLICK_PLAY_JS), CLICK_TIMEOUT):
        raise fail(plan, "battlenetLaunchNotObserved",
                   "Battle.net's Play button never became clickable", uid=uid)
    logger.info("[battlenet] pressed Play in the client for %s", uid)
    if not await _poll(
        lambda: count_in_logs(plan.prefix_path, launched) > launches_before, LAUNCH_TIMEOUT,
    ):
        raise fail(plan, "battlenetLaunchNotObserved",
                   f"Battle.net did not launch {uid} after Play", uid=uid)
    pid = await watch.wait_for_game(plan.prefix_path, before, bnet_exec.GAME_APPEAR_TIMEOUT)
    if pid is None:
        raise fail(plan, "battlenetLaunchNotObserved",
                   f"Battle.net launched {uid} but no game process appeared", uid=uid)
    return pid, before
