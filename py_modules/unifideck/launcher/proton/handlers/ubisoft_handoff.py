"""Serialised UPC session handoff around a Ubisoft game launch.

Ubisoft rotates the refresh token on every sign-in and retires the one
before it. With a prefix per game that makes the session a *baton*, not a
snapshot: whichever prefix ran last holds the only live token, and every
other copy is dead. Passing it correctly needs two moves, and both belong
here in the launcher because this is the process that brackets UPC:

* **before** UPC starts — seed the prefix with the newest known session,
  so the game runs on the live token instead of whatever it kept from
  its own last run;
* **after** UPC exits — capture the token UPC just rotated and fan it
  back out, so the *next* game gets a live one.

The backend has a ``GAME_STOPPED`` subscriber doing the second half
already (``stores/ubisoft/store.py``). It is kept, and this is not
redundant with it: that event is bridged from the frontend's Steam
app-state watcher, and when it does not arrive — plugin restart, a missed
state change — the token is stranded in the game prefix and the symptom is
exactly the bug this module exists to fix (GH #435). Doing it here as well
makes the handoff independent of the frontend. Both paths are idempotent:
``capture`` is mtime-gated, so the second one to run is a no-op.

Every function here is best-effort. A launch must not fail because a
credential could not be moved — the worst case is the sign-in prompt the
user would have got anyway. Imports are deferred into the call bodies
because this module is imported by the launcher under the *system* python.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: UPC flushes its rotated token as it shuts down, so reading the vault the
#: instant the game process returns gets the token from BEFORE the run — or a
#: torn file. Bounded, because a UPC that refuses to die must not hold the
#: launcher open behind the game.
_EXIT_WAIT_SECONDS = 20.0
_EXIT_POLL_SECONDS = 1.0

#: Duplicated from ``stores.ubisoft.config`` rather than imported: this runs
#: under the system python before the store package is needed, and the
#: fingerprint has to work even if that import fails.
_UPC_LOCAL_SUBDIR = "AppData/Local/Ubisoft Game Launcher"
_VAULT_NAME = "ConnectSecureStorage.dat"


def seed_before_launch(prefix_dir: Path) -> None:
    """Copy the newest known UPC session into ``prefix_dir`` before it runs.

    ``inject_into_prefix`` picks the best source and will not overwrite a
    target that already holds a newer signed-in session, so a prefix that is
    already current is left alone (the copy is hashed first, so this is cheap
    and idempotent).
    """
    try:
        from unifideck.stores.ubisoft.session import build_standalone_session

        if build_standalone_session().inject_into_prefix(str(prefix_dir)):
            logger.info(
                "[launcher.proton.ubisoft] seeded UPC session into %s",
                prefix_dir.name,
            )
    except Exception:
        logger.exception(
            "[launcher.proton.ubisoft] session seed into %s failed "
            "(non-fatal; UPC may ask the user to sign in)",
            prefix_dir,
        )


def vault_fingerprint(prefix_dir: Path) -> tuple[int, int] | None:
    """``(size, mtime_ns)`` of this prefix's vault, or None if it has none.

    Taken before the run and compared after, so a capture can require
    positive evidence that UPC *wrote* the vault. See
    :func:`capture_after_exit`.
    """
    for rel in (
        "drive_c/users/steamuser",
        "pfx/drive_c/users/steamuser",
        "drive_c/users/deck",
        "pfx/drive_c/users/deck",
    ):
        vault = prefix_dir / rel / _UPC_LOCAL_SUBDIR / _VAULT_NAME
        try:
            st = vault.stat()
        except OSError:
            continue
        return (st.st_size, st.st_mtime_ns)
    return None


def capture_after_exit(
    prefix_dir: Path,
    before: tuple[int, int] | None = None,
) -> None:
    """Capture the token UPC rotated in ``prefix_dir`` and fan it back out.

    ``before`` is the vault fingerprint taken *before* UPC ran. The capture
    only proceeds when the vault actually changed during the run, because a
    capture is a claim that UPC produced a session — and only UPC writing the
    file is evidence of that.

    Without this check the capture fans out whatever the prefix happens to
    hold. Measured on-device: a prefix whose vault had been corrupted was
    launched, UPC failed to use it and exited rc=1 without rewriting anything,
    and the unchanged corrupt file was then captured into the auth prefix and
    propagated to every other game — the exact "one bad prefix poisons them
    all" incident this layer exists to prevent. The file was a plausible size
    and had its ``user.dat``, so neither the size heuristic nor the signed-in
    shape test could see anything wrong. Nothing about the *content* of a
    vault can be validated locally; whether UPC rewrote it can be.
    """
    try:
        from unifideck.stores.ubisoft.session import build_standalone_session

        _await_upc_exit(prefix_dir)
        after = vault_fingerprint(prefix_dir)
        if after is None:
            return
        if before is not None and after == before:
            logger.info(
                "[launcher.proton.ubisoft] %s: UPC left the vault untouched "
                "— nothing to capture",
                prefix_dir.name,
            )
            return
        session = build_standalone_session()
        if session.capture(str(prefix_dir)):
            session.propagate_all_to_all()
            logger.info(
                "[launcher.proton.ubisoft] captured rotated UPC token from "
                "%s → auth refreshed",
                prefix_dir.name,
            )
    except Exception:
        logger.exception(
            "[launcher.proton.ubisoft] post-run session capture from %s "
            "failed (non-fatal)",
            prefix_dir,
        )


def _await_upc_exit(prefix_dir: Path) -> None:
    """Wait, bounded, for UPC to be gone from ``prefix_dir``.

    Mirrors the backend's ``wrapper_session_hooks.await_client_exit``; this
    one is synchronous because the launch handler awaits it off the event
    loop and the backend's version is ``async``.
    """
    try:
        from unifideck.launcher.proton.handlers.wrapper_clients import (
            client_running_in,
        )
    except Exception:
        return
    waited = 0.0
    while waited < _EXIT_WAIT_SECONDS:
        try:
            if not client_running_in("ubisoft", prefix_dir):
                return
        except Exception:
            return
        time.sleep(_EXIT_POLL_SECONDS)
        waited += _EXIT_POLL_SECONDS
    logger.info(
        "[launcher.proton.ubisoft] UPC still up in %s after %.0fs — "
        "capturing anyway",
        prefix_dir.name,
        _EXIT_WAIT_SECONDS,
    )
