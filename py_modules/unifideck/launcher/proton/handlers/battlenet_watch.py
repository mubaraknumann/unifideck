"""Process observation for the Battle.net two-phase launch.

py_modules/unifideck/launcher/proton/handlers/battlenet_watch.py

Everything here runs on the **Linux side**, reading ``/proc``. That is a
deliberate anti-cheat hygiene rule, not incidental: Warden scans the game
process's memory, its loaded code, the Windows process list and its handle
table. Reading ``/proc/<pid>/cmdline`` and ``/environ`` touches none of
those — it never enters the prefix, never opens a handle to the game, and
never appears in the Windows process list.

Two measured facts shape the probes:

* **There is no ``Battle.net Helper.exe`` process.** That string is a
  command-line *argument*; every CEF child is named ``Battle.net.exe`` and
  distinguished by ``--type=``. An earlier design keyed readiness on a
  process that does not exist.
* **The client's ``WINEPREFIX`` is ``<prefix>/pfx/``**, because umu
  normalises it and creates ``pfx -> .`` as a self-symlink. Prefix matching
  must therefore normalise, or a client running for a sibling Blizzard game
  is mistaken for this one's.

The ``/proc`` primitives themselves now live in ``wrapper_clients``: they are
not Battle.net-specific, and the question that motivated moving them — "is a
client already running in some *other* prefix" — cannot be asked from a
module whose every function takes one prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from .wrapper_clients import CLIENT_IMAGES, kill_client, terminate
from .wrapper_clients import scan_prefix as _scan_raw

logger = logging.getLogger(__name__)

# The client's own processes and Wine's infrastructure. None of these is
# ever "the game started". Measured during a real 12.43 GB install.
EXCLUDED_IMAGES: frozenset[str] = frozenset({
    "battle.net.exe",
    "battle.net launcher.exe",
    "agent.exe",
    "agenthelper.exe",
    "blizzarderror.exe",
    "blizzardbrowser.exe",
    "blizzard uninstaller.exe",
    # Wine / Proton infrastructure
    "explorer.exe",
    "services.exe",
    "winedevice.exe",
    "plugplay.exe",
    "rpcss.exe",
    "svchost.exe",
    "tabtip.exe",
    "conhost.exe",
    "wineboot.exe",
    "start.exe",
    "winemenubuilder.exe",
    "umu.exe",
    "xalia.exe",
    "steam.exe",
})

# The client's CEF children carry --type=; the main process carries none.
_RENDERER = "--type=renderer"

# The client's own images, for teardown. Distinct from EXCLUDED_IMAGES,
# which additionally covers Wine infrastructure we must never signal, and
# shared with ``wrapper_clients`` so the cross-prefix scan and the teardown
# agree on what "the client" is.
_CLIENT_IMAGES: frozenset[str] = CLIENT_IMAGES["battlenet"]


def scan(prefix: str | Path) -> list[tuple[str, str]]:
    """``(pid, image_name)`` for every Windows process in this prefix."""
    return [(pid, image) for pid, image, _ in _scan_raw(prefix)]


def _client_pids(prefix: str | Path) -> tuple[list[str], list[str]]:
    """``(all_client_pids, renderer_pids)`` for the client in this prefix.

    One scan answering both questions, because they are asked together and
    ``/proc`` is the expensive part.

    *Every* client image counts, whatever its ``--type=``. This used to
    require ``--from-launcher`` or ``--type=renderer``, which meant that
    once the main process died the surviving ``--type=gpu-process`` and
    ``--type=utility`` children matched nothing: :func:`stop_client`
    signalled zero, the dead session stayed in the prefix, and because
    ``client_ready`` was then False the next launch started a *second*
    full client on top of it. Two stacked sessions were measured on-device.
    """
    everything: list[str] = []
    renderers: list[str] = []
    for pid, image, cmdline in _scan_raw(prefix):
        if image != "battle.net.exe":
            continue
        everything.append(pid)
        if _RENDERER in cmdline:
            renderers.append(pid)
    return everything, renderers


def client_ready(prefix: str | Path) -> bool:
    """True once the client can accept an ``--exec`` command.

    Keyed on a CEF renderer being up, which is the Linux-observable
    equivalent of "the main window exists". A window probe is not usable:
    xdotool cannot see into Gaming Mode's separate gamescope session.

    **Every candidate is examined before concluding "no".** This used to
    ``return`` the verdict for whichever process ``/proc`` yielded first,
    and the ``--from-launcher`` main process starts first (so gets a lower
    pid) and is not a renderer — so the probe answered False while two
    renderers were running. Measured on-device: pid 69087 (main) shadowed
    69473 and 69551 (both renderers), the client never became "ready", and
    every launch failed after the full 300 s timeout.
    """
    return bool(_client_pids(prefix)[1])


def client_running(prefix: str | Path) -> bool:
    """True while *any* client process is alive in this prefix.

    Deliberately weaker than :func:`client_ready`. Readiness asks "can it
    accept a command yet"; liveness asks "is it still up". Using readiness
    to decide when to stop waiting ends the wait during a client restart or
    an update pass, when the renderer is momentarily gone but the client is
    very much still running.
    """
    return bool(_client_pids(prefix)[0])


def game_pids(prefix: str | Path) -> set[str]:
    """PIDs of non-excluded Wine processes — candidate game processes."""
    return {pid for pid, image in scan(prefix) if image not in EXCLUDED_IMAGES}


def wine_pids(prefix: str | Path) -> list[str]:
    """Every Windows process in this prefix, infrastructure included.

    The liveness question :func:`client_running` cannot answer: a prefix
    holding only ``Agent.exe`` and ``services.exe`` has no client left,
    but its wineserver still blocks the next phase A's ``wineserver -w``.
    """
    return [pid for pid, _ in scan(prefix)]


def _client_gave_up(prefix: str | Path, proc: object | None) -> bool:
    """Whether the run we started has exited leaving nothing behind.

    Both halves are required and neither is sufficient:

    * the phase-A process having exited is normal on its own — umu execs
      through several wrappers and the client is detached, so the outer
      process can return while Wine keeps running;
    * an empty prefix is normal on its own for the first few seconds, before
      ``wineboot`` has started anything.

    Together they mean the attempt is over. Measured in the field: a client
    started, exited within ~20 s and left the prefix empty, and the wait sat
    out its remaining 4½ minutes before reporting a timeout — which reads as
    "still starting" and sent the diagnosis toward the wrong half of the
    system entirely.
    """
    if proc is None or getattr(proc, "returncode", None) is None:
        return False
    return not wine_pids(prefix)


async def wait_for_client_ready(
    prefix: str | Path,
    deadline_seconds: float,
    poll: float = 2.0,
    proc: object | None = None,
) -> bool:
    """Poll until the client's renderer appears, or give up.

    ``proc`` is phase A's process handle, when the caller has one. It turns
    the give-up condition from "the deadline passed" into "the deadline
    passed, or the client is provably gone" — see :func:`_client_gave_up`.
    """
    timeout = deadline_seconds
    waited = 0.0
    while waited < timeout:
        if client_ready(prefix):
            logger.info("[battlenet] client ready after %.0fs", waited)
            return True
        if _client_gave_up(prefix, proc):
            logger.error(
                "[battlenet] client exited after %.0fs (rc=%s) without starting — "
                "no Wine processes left in %s; see the game log for its output",
                waited, getattr(proc, "returncode", "?"), prefix,
            )
            return False
        await asyncio.sleep(poll)
        waited += poll
    logger.error("[battlenet] client not ready after %.0fs", timeout)
    return False


class ReadinessLatch:
    """Remembers whether this prefix's client was ever seen up.

    :func:`client_ready` can only answer about *now*, and the question that
    matters after a run ends is whether the window ever appeared. Sign-in asks
    it to tell two identical-looking exits apart: a client that aborted during
    renderer init (the ANGLE/gamescope crash a retry exists for) never becomes
    ready, while a client the user opened and then closed did. Retrying the
    second reopens a window they just dismissed.

    Latching, never clearing. The client drops its renderer briefly during a
    self-update, so a live reading taken at exit time would report a
    user-closed client as one that never started.
    """

    def __init__(self) -> None:
        self.seen = False

    async def poll(self, prefix: str | Path, interval: float = 2.0) -> None:
        """Watch until readiness is observed. Cancelled by the caller."""
        while not self.seen:
            if client_ready(prefix):
                self.seen = True
                return
            await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def watch_readiness(
    prefix: str | Path, interval: float = 2.0,
) -> AsyncGenerator[ReadinessLatch]:
    """Run a :class:`ReadinessLatch` for the duration of the block."""
    latch = ReadinessLatch()
    task = asyncio.create_task(latch.poll(prefix, interval))
    try:
        yield latch
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def wait_for_game(
    prefix: str | Path,
    before: set[str],
    deadline_seconds: float,
    poll: float = 3.0,
) -> str | None:
    """Wait for a game process that was not running before. None on timeout.

    This is the silent-failure detector. An obsolete family code makes the
    client accept ``--exec="launch X"`` and do nothing at all — no error, no
    dialog, no exit code — so "the command returned" proves nothing and only
    a new process does.
    """
    timeout = deadline_seconds
    waited = 0.0
    while waited < timeout:
        appeared = game_pids(prefix) - before
        if appeared:
            # Numeric, not lexicographic: sorted() on pid *strings* puts
            # "10000" before "9999".
            pid = min(appeared, key=int)
            logger.info("[battlenet] game process %s appeared after %.0fs", pid, waited)
            return pid
        await asyncio.sleep(poll)
        waited += poll
    return None


def _game_still_running(prefix: str | Path, pid: str) -> bool:
    """Whether ``pid`` is still a live game process in this prefix."""
    return Path(f"/proc/{pid}").exists() and pid in game_pids(prefix)


def _any_game_running(prefix: str | Path, pid: str, before: set[str]) -> bool:
    """Whether ``pid`` — or any game process that replaced it — is alive.

    Following one pid is not enough. Blizzard titles hand off: for Diablo
    II: Resurrected the client starts ``Diablo II Resurrected Launcher.exe``,
    which exits once ``D2R.exe`` is up. Watching only the first pid ends
    the wait seconds in, so Steam marks the shortcut stopped while the
    game is still running.

    ``before`` is the phase-D snapshot, so a process that predates this
    launch never counts as our game.
    """
    return _game_still_running(prefix, pid) or bool(game_pids(prefix) - before)


async def wait_for_exit(
    prefix: str | Path, pid: str, *, before: set[str], poll: float = 10.0,
) -> None:
    """Block until the game — and anything it handed off to — goes away.

    Polls rather than waits on an event: the game is not our child (the
    client spawned it inside the prefix), so there is no handle to await
    and nothing in-process will ever signal us. ``asyncio.Event`` would
    have nobody to set it.
    """
    while _any_game_running(prefix, pid, before):  # noqa: ASYNC110 — external OS state
        await asyncio.sleep(poll)


async def wait_while_client_running(prefix: str | Path, poll: float = 10.0) -> None:
    """Block while the client is up, so Steam keeps the shortcut alive.

    Same reasoning as :func:`wait_for_exit`: the client is a detached
    process we do not own.

    Waits on *liveness*, not readiness. Keyed on ``client_ready`` this
    returned on the first poll — the readiness probe was answering False
    for a running client — so Steam saw the install shortcut exit
    immediately while the detached client stayed up: the tile stopped
    responding, the playtime session never closed, and the window's "X"
    had nothing left listening to it.
    """
    while client_running(prefix):  # noqa: ASYNC110 — external state, no event source
        await asyncio.sleep(poll)


def stop_client(prefix: str | Path, *, timeout: float = 15.0) -> int:
    """Terminate the client running in ``prefix``. Returns how many were signalled.

    Scoped to this prefix by ``WINEPREFIX``, and to the **client's own
    images** — never the whole Wine session. ``Agent.exe`` is deliberately
    spared: this runs from ``_client_teardown``, which also wraps the
    install flow, and killing the Agent mid-download is the exact failure
    this module was fixed for. Use :func:`stop_stale_session` when the
    intent really is to clear the prefix.

    Delegates to the shared, table-driven :func:`wrapper_clients.kill_client`,
    which selects exactly the same images via ``CLIENT_IMAGES["battlenet"]``.
    """
    return kill_client("battlenet", prefix, timeout=timeout)


def _launched_game_pids(prefix: str | Path, pid: str | None, before: set[str]) -> list[str]:
    """This launch's game processes: ``pid`` and any game that replaced it."""
    return sorted(p for p in game_pids(prefix) if p == pid or p not in before)


def stop_game(
    prefix: str | Path, pid: str | None, before: set[str], *, timeout: float = 15.0,
) -> int:
    """Terminate the game this launch started. Returns how many were signalled.

    The Steam stop button needs this and nothing else provides it. The game
    is a child of the client's Wine session, whose pressure-vessel container
    starts its own session, so Steam's reaper and the umu-run ``killpg``
    both miss it. :func:`stop_client` spares it on purpose. Measured
    2026-09-24: after a stop, ``Warcraft III.exe`` kept running with 27
    processes around it. One SIGTERM to the game let the whole session wind
    down, the Agent and wineserver included, within 15 s.

    ``before`` is the phase-D snapshot, the same scoping as
    :func:`wait_for_exit`: a game that predates this launch is never ours.
    """
    pids = _launched_game_pids(prefix, pid, before)
    if not pids:
        return 0
    logger.info("[battlenet] stopping %d game process(es) in %s", len(pids), prefix)
    return terminate(
        pids, lambda: _launched_game_pids(prefix, pid, before), timeout, label="battlenet",
    )


def stop_stale_session(prefix: str | Path, *, timeout: float = 15.0) -> int:
    """Clear an entire dead Wine session out of ``prefix``.

    For the case :func:`stop_client` must not handle: a session with no
    usable client left, whose surviving Wine infrastructure still holds
    the wineserver that phase A's ``waitforexitandrun`` would block on.
    Signals every Windows image, then reaps the wineserver itself — by
    then we own the prefix, which is what that reap requires.
    """
    pids = wine_pids(prefix)
    if not pids:
        return 0
    logger.warning(
        "[battlenet] clearing stale session: %d process(es) in %s", len(pids), prefix,
    )
    stopped = terminate(
        pids, lambda: wine_pids(prefix), timeout, label="battlenet",
    )
    with contextlib.suppress(Exception):
        from unifideck.launcher.proton.infrastructure.wineserver_reap import (
            reap_prefix_wineserver,
        )
        reap_prefix_wineserver(Path(prefix))
    return stopped
