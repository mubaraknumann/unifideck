"""Whether the Battle.net client in a prefix is actually signed in.

py_modules/unifideck/launcher/proton/handlers/battlenet_login_state.py

``battlenet_watch.client_ready`` answers "can the client accept a command",
keyed on a CEF renderer existing. That is also true while the client sits on
the login page, and the two questions came apart on-device: phase C sent
``--exec="launch D1"`` into a client whose log recorded
``Login failed. error=ERROR_TOKEN_NOT_FOUND (49)`` seconds earlier, the
command was accepted, nothing started, and the launch failed 180 s later as
``battlenetLaunchNotObserved`` — which blames a family-code rename for what
is really a signed-out client. Four such toasts were in one device's
``launcher_events.jsonl``.

The signal is the client's **own log**. Reading it keeps this module inside
``battlenet_watch``'s rule that everything runs on the Linux side: no handle
into the prefix, nothing in the Windows process list, nothing Warden can
see. Both markers below were measured by diffing a successful session
against two failed ones on this Deck:

* success -- ``[BNLogin] Logged into Battle.net successfully.``
* failure -- ``[BNLogin] Login failed. error=`` and
  ``UAuth: browser state changed: LoginCredential``

Checked, because the obvious alternative is wrong: the *successful* session
also logs ``UAuth: setting url: https://account.battle.net/login/...``, so
that line proves nothing.

Three-valued on purpose. A client whose log has neither marker yet is
``UNKNOWN``, and a caller must never block on ``UNKNOWN`` — the whole point
is to catch a *proven* signed-out client, not to add a new way for a launch
to fail on a log format that changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import Enum
from pathlib import Path

from unifideck.launcher.proton.infrastructure.prefix_layout import (
    resolve_drive_c,
)

logger = logging.getLogger(__name__)

_LOG_DIR = "users/steamuser/AppData/Local/Battle.net/Logs"
_LOG_GLOB = "battle.net-*.log"

_SIGNED_IN_MARKER = "Logged into Battle.net successfully"
_SIGNED_OUT_MARKERS = (
    "Login failed. error=",
    "browser state changed: LoginCredential",
)
# Written by the ``--exec`` handoff, which opens the IPC queue, forwards its
# argument and exits. It is a real log file and it is the newest one on disk
# right after a phase C, but it says nothing about the session — so skipping
# it is what lets this work for an already-running client.
_HANDOFF_MARKER = "Leaving because another instance of battle.net is running"

# A client log is ~1.5k short lines. Bounded anyway: this is parsing a
# vendor's file, and an unbounded read of one is how a wedged client with a
# runaway log takes the launcher down with it.
_MAX_LOG_BYTES = 4 * 1024 * 1024


class LoginState(Enum):
    """What the client's log says about its session."""

    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    UNKNOWN = "unknown"


def _log_dir(prefix: Path | str) -> Path | None:
    """The client's log directory inside ``prefix``, if the prefix has one."""
    drive_c = resolve_drive_c(Path(prefix))
    if drive_c is None:
        return None
    logs = drive_c / _LOG_DIR
    return logs if logs.is_dir() else None


def _newest_session_log(prefix: Path | str) -> Path | None:
    """Newest client log in ``prefix`` that is not an ``--exec`` handoff."""
    logs = _log_dir(prefix)
    if logs is None:
        return None
    candidates: list[tuple[float, Path]] = []
    for path in logs.glob(_LOG_GLOB):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        if not _is_handoff(path):
            return path
    return None


def _is_handoff(path: Path) -> bool:
    """Whether ``path`` is the tiny log an ``--exec`` invocation leaves."""
    text = _read(path)
    return _HANDOFF_MARKER in text


def _read(path: Path) -> str:
    """``path``'s text, bounded and never raising."""
    with contextlib.suppress(OSError):
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(_MAX_LOG_BYTES)
    return ""


def read_login_state(prefix: Path | str) -> LoginState:
    """What the newest client session in ``prefix`` says about its login.

    The **last** decisive marker wins. A session that fails on an expired
    token and then succeeds once the user types their password logs both, in
    that order, and it is signed in.
    """
    path = _newest_session_log(prefix)
    if path is None:
        return LoginState.UNKNOWN
    state = LoginState.UNKNOWN
    for line in _read(path).splitlines():
        if _SIGNED_IN_MARKER in line:
            state = LoginState.SIGNED_IN
        elif any(marker in line for marker in _SIGNED_OUT_MARKERS):
            state = LoginState.SIGNED_OUT
    return state


async def wait_for_login(
    prefix: Path | str,
    deadline_seconds: float,
    poll: float = 3.0,
    settle_seconds: float = 30.0,
) -> LoginState:
    """Wait for the client to be signed in, or report what it is instead.

    Two budgets, because the two states mean different things:

    * ``SIGNED_OUT`` gets the full ``deadline_seconds``. The login page is on
      screen in front of the user and is their chance to fix exactly this, so
      the wait rides it out rather than failing them mid-typing.
    * ``UNKNOWN`` gets only ``settle_seconds`` — enough for a client that has
      not written its verdict yet, and no more. Waiting the full deadline on
      *absence* of evidence would stall every launch whose log we cannot read
      by three minutes, which is a far worse bug than the one being fixed.
      A client that is up signs in ~10s after its renderer appears.
    """
    # No session log at all is not "not yet" — it is nothing to wait for.
    # The caller has already waited for a CEF renderer, which the client
    # reaches long after it opens its log, so a prefix with no log here is
    # one whose logs we simply cannot read.
    if await asyncio.to_thread(_newest_session_log, prefix) is None:
        return LoginState.UNKNOWN
    waited = 0.0
    announced = False
    state = LoginState.UNKNOWN
    while waited < deadline_seconds:
        state = await asyncio.to_thread(read_login_state, prefix)
        if state is LoginState.SIGNED_IN:
            logger.info("[battlenet] client signed in after %.0fs", waited)
            return state
        if state is LoginState.UNKNOWN and waited >= settle_seconds:
            logger.info(
                "[battlenet] no login verdict in the client log after %.0fs — "
                "proceeding", waited,
            )
            return state
        if state is LoginState.SIGNED_OUT and not announced:
            announced = True
            logger.warning(
                "[battlenet] client is on the login page — waiting up to %.0fs "
                "for sign-in", deadline_seconds,
            )
        await asyncio.sleep(poll)
        waited += poll
    return state
