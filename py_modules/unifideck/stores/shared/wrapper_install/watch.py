"""Watching a vendor client install a game, for any wrapper store.

py_modules/unifideck/stores/shared/wrapper_install/watch.py

The install itself is a user click inside the vendor's own Windows client. We
do not run it, we cannot measure it, and there is no process of ours to wait
on — the client is started by the frontend through Steam's ``RunGame`` so it
gets a gamescope session and actually renders in Gaming Mode. All the backend
can do is watch the prefix and decide, from the outside, when the game is
there and when the attempt has been abandoned.

This was Ubisoft's, privately, and every constant in it is a scar:

* **Completion is not "the client says so".** Neither vendor prints a
  "finished" message, so each store recognises it from its own prefix state:
  Battle.net reads ``product.db``, Ubisoft reads whether UPC has drained its
  staging directory. ``InstallProbe.is_complete`` is asked first and the size
  heuristic is only the fallback for a probe that answers ``None``.
* **The size heuristic is a last resort, and it is dangerous.** It ended a
  Ubisoft install 18 minutes early, because ``measure`` was returning apparent
  size and UPC pre-allocates the whole game as sparse files the moment it
  accepts the job — so "three unchanged reads" was satisfied while 2% of the
  bytes had landed. A probe must measure allocated blocks, and should answer
  ``False`` rather than ``None`` whenever it knows a download is still running.
* **Never kill the client on completion.** Only on cancel. Because completion
  can be inferred early, killing on it interrupted still-running installs —
  users watched Ubisoft Connect close mid-install and resume on reopen.
* **A tray'd client is not a closed one.** Ubisoft Connect minimises to tray
  during long downloads, so a window probe reports it gone. Liveness is read
  from ``/proc`` instead, which also works in Gaming Mode where a window probe
  is blind to the separate gamescope session.
* **"Never started" needs its own watchdog.** The client-quit watchdog is
  gated on having seen the client, so when it never comes up at all that flag
  stays False and the install burned the full two-hour timeout showing
  "Follow the launcher window" — indistinguishable from a hang. That happened
  in the field: a failed ``RunGame`` (the title missing from ``games.map``)
  left exactly this state.
* **A probe that cannot answer must never end an install.** Every liveness
  failure is read as "alive".

Battle.net used to skip all of this — it reported success the moment its prefix
was placed, so a game showed a Play button before a single byte was downloaded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from unifideck.launcher.proton.handlers.wrapper_clients import (
    install_active_in,
    live_client_prefixes,
)

from .probe import InstallProbe

logger = logging.getLogger(__name__)

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]] | None

# Defaults, overridable per store by an ``InstallProbe`` attribute of the same
# lower-cased name. A manual install is bounded only by the user's patience;
# two hours is the ceiling before we stop holding the queue slot open.
INSTALL_TIMEOUT_S = 2 * 60 * 60
POLL_INTERVAL_S = 10.0

# Completion-by-stability, for stores with no authoritative signal: three
# consecutive unchanged non-zero size reads.
#
# ``STABILITY_MAX_POLLS`` is only the *fallback* budget for the download wait.
# It used to be the whole budget, and it silently contradicted the per-store
# timeout: 360 polls at Battle.net's 15 second interval is 90 minutes, against
# the 4 hours its probe declares. The clock starts when ``detect()`` first sees
# the install directory, and a vendor client creates that when it *accepts* the
# job rather than when bytes start landing, so a long queue wait plus a large
# download ran past 90 minutes and a healthy install was reported as failed.
# The budget now derives from the probe's own timeout; this constant applies
# only to a probe that declares neither.
STABILITY_MAX_POLLS = 360
STABILITY_THRESHOLD = 3

# Seconds with the client gone, after it was seen at least once, before the
# attempt counts as abandoned. Generous on purpose: it must outlast a client
# restarting itself, which both vendors do — Ubisoft Connect hands off from its
# first-run installer to the main launcher, and Battle.net restarts to apply
# its own updates.
CLIENT_GONE_GRACE_S = 180.0

# Seconds at the START with no client at all before we conclude it will never
# come up. Comfortably longer than a cold client start under Proton, short
# enough to be actionable.
NEVER_STARTED_GRACE_S = 90.0

# Emit a "still waiting" tick every N polls (≈ 1 min) — enough to keep the UI
# alive without spamming the bus on every poll.
TICK_EVERY_POLLS = 6


def install_alive(store: str, prefix: str | Path) -> bool:
    """Whether ``store`` is still installing into ``prefix``.

    Prefix-scoped first, then anywhere. The second half looks careless and is
    not: the watchdogs this feeds can only ever *end* an install, so the fail
    direction must be "alive". A client whose ``WINEPREFIX`` we cannot match —
    an unusual launch path, a prefix relocated mid-flight — would otherwise
    read as gone and abandon a download in progress. It also preserves the
    behaviour of the global ``pgrep`` this replaced.

    An unreadable ``/proc`` likewise reports alive, never dead.
    """
    try:
        if install_active_in(store, prefix):
            return True
        return bool(live_client_prefixes(store))
    except Exception:
        logger.debug("[%s] install liveness probe failed", store, exc_info=True)
        return True


class _Watch:
    """One manual-install watch. Holds the poll counters so the loop stays flat."""

    def __init__(
        self,
        probe: InstallProbe,
        prefix: str | Path,
        progress_cb: ProgressCb,
    ) -> None:
        self._probe = probe
        self._prefix = prefix
        self._progress_cb = progress_cb
        self._poll = float(getattr(probe, "poll_interval_s", POLL_INTERVAL_S))
        self._timeout = float(getattr(probe, "timeout_s", INSTALL_TIMEOUT_S))
        self._gone_grace = float(
            getattr(probe, "client_gone_grace_s", CLIENT_GONE_GRACE_S),
        )
        self._never_grace = float(
            getattr(probe, "never_started_grace_s", NEVER_STARTED_GRACE_S),
        )
        # How many polls the download wait may take. Derived from the same
        # timeout the appear loop uses, so a store cannot declare 4 hours and
        # silently get 90 minutes. Never shorter than the old fixed budget: a
        # store with a short timeout should not lose download headroom it
        # already had.
        self._completion_polls = max(
            int(self._timeout / self._poll), STABILITY_MAX_POLLS,
        )
        self._seen = False
        self._gone_for = 0.0
        self._absent_for = 0.0

    async def _emit(self, message: str) -> None:
        """Push an indeterminate manual-phase progress update."""
        if self._progress_cb is None:
            return
        await self._progress_cb({"phase": "manual", "phase_message": message})

    def _explained_wait(self) -> str | None:
        """The store's own account of why nothing is moving, if it has one.

        Optional per store. See the note in :mod:`.probe` on why this is read
        with ``getattr`` rather than declared on the protocol. Any failure is
        swallowed: a progress *message* must never be able to end an install.
        """
        describe: Callable[[], str | None] | None = getattr(
            self._probe, "status_message", None,
        )
        if describe is None:
            return None
        try:
            message = describe()
            return message if isinstance(message, str) and message else None
        except Exception:
            logger.debug(
                "[%s] status_message failed", self._probe.store, exc_info=True,
            )
            return None

    def _abandoned(self) -> bool:
        """Run both give-up watchdogs for one poll.

        They cover disjoint situations and neither substitutes for the other:
        one catches "the client was up and the user quit it", the other "the
        client never came up at all". Only ever consulted while waiting for the
        game to *appear* — once it is on disk, a download finishes whether or
        not the client window is still up.
        """
        if install_alive(self._probe.store, self._prefix):
            self._seen = True
            self._gone_for = 0.0
            self._absent_for = 0.0
            return False
        if not self._seen:
            return self._never_started()
        self._gone_for += self._poll
        if self._gone_for < self._gone_grace:
            return False
        logger.info(
            "[%s] %s gone for ~%.0fs — treating the install as abandoned",
            self._probe.store,
            self._probe.client_label,
            self._gone_for,
        )
        return True

    def _never_started(self) -> bool:
        """Count early time with no client at all; True once we give up."""
        self._absent_for += self._poll
        if self._absent_for < self._never_grace:
            return False
        logger.error(
            "[%s] %s never started (%ds with no client process). The install "
            "was waiting for a window that will never appear — check the "
            "launcher log for this game; a failed RunGame (e.g. the title "
            "missing from games.map) leaves exactly this state.",
            self._probe.store,
            self._probe.client_label,
            int(self._absent_for),
        )
        return True

    async def start(
        self,
        on_ready: Callable[[], Awaitable[None]] | None,
    ) -> str | None:
        """Snapshot, ask the frontend to open the client, then watch.

        The ordering is load-bearing: ``on_ready`` is what triggers the
        frontend's ``RunGame``, and a baseline captured after it could miss a
        directory the client creates on startup.
        """
        baseline = self._probe.snapshot()
        await self._emit(
            f"{self._probe.client_label} is opening — install the game from its window.",
        )
        logger.info(
            "[%s] awaiting %s launch via frontend RunGame",
            self._probe.store,
            self._probe.client_label,
        )
        if on_ready is not None:
            await on_ready()
        return await self._run(baseline)

    async def _run(self, baseline: Any) -> str | None:
        """Poll until the game appears and finishes, or the attempt is over."""
        max_polls = int(self._timeout / self._poll)
        for iteration in range(max_polls):
            await asyncio.sleep(self._poll)
            install_dir = self._probe.detect(baseline)
            if install_dir:
                logger.info(
                    "[%s] detected install at %s",
                    self._probe.store,
                    install_dir,
                )
                await self._emit(
                    f"Installing {Path(install_dir).name} via {self._probe.client_label}…",
                )
                if not await self._await_completion(install_dir):
                    return None
                return install_dir
            if self._abandoned():
                return None
            if iteration % TICK_EVERY_POLLS == 0:
                await self._emit(
                    self._explained_wait()
                    or f"Waiting for the game to install in "
                       f"{self._probe.client_label}…",
                )
        logger.warning(
            "[%s] manual install timed out after %.1fh",
            self._probe.store,
            self._timeout / 3600,
        )
        return None

    async def _await_completion(self, install_dir: str) -> bool:
        """Block until the install finishes. True only when it actually did.

        The store's own verdict wins whenever it has one — including a
        ``False`` verdict, which keeps the size heuristic from ending a
        download that has merely paused.

        Running out of polls is a **failure**, not a completion. This
        returned ``None`` unconditionally, so the caller took "the loop
        ended" as "the game is installed" and recorded a game that had
        never finished downloading — a Play button on an install the
        vendor client had left part-written. A store that cannot say yes
        must not be reported as yes.
        """
        prev_size = 0
        stable = 0
        for _ in range(self._completion_polls):
            await asyncio.sleep(self._poll)
            verdict = self._probe.is_complete(install_dir)
            if verdict is True:
                logger.info(
                    "[%s] %s reports the install complete",
                    self._probe.store,
                    self._probe.client_label,
                )
                return True
            size = self._probe.measure(install_dir)
            if verdict is None:
                stable = stable + 1 if size == prev_size and size > 0 else 0
                if stable >= STABILITY_THRESHOLD:
                    return True
            prev_size = size
            # The explained wait wins over the byte count here, not just in
            # the appear loop. A vendor client creates the install directory
            # when it *accepts* the job, not when it starts writing, so a game
            # queued behind the client's own self-update sits at a few KB for
            # as long as that takes, and "Installing… (0.0 GB)" is a worse
            # answer than silence, let alone than naming what is in front of
            # it. Measured: 28 minutes of exactly this.
            explained = self._explained_wait()
            if explained:
                await self._emit(explained)
            elif size > 0:
                await self._emit(f"Installing… ({size / (1024**3):.1f} GB)")
        logger.warning(
            "[%s] %s never reported this install complete after %d polls "
            "(~%.1fh). The files are on disk but part-written, so this is "
            "reported as a failure rather than recorded as installed.",
            self._probe.store,
            self._probe.client_label,
            self._completion_polls,
            self._completion_polls * self._poll / 3600,
        )
        return False


async def watch_manual_install(
    *,
    probe: InstallProbe,
    prefix: str | Path,
    progress_cb: ProgressCb = None,
    on_ready: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    """Watch a vendor-client install through to completion.

    Returns the host-side install directory, or ``None`` when the attempt was
    abandoned or timed out. Cancellation propagates untouched — closing the
    client and capturing its rotated session are the caller's business, because
    only the caller knows whether its prefix is about to be destroyed.
    """
    return await _Watch(probe, prefix, progress_cb).start(on_ready)
