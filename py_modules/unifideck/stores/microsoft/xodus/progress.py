"""stores/microsoft/xodus/progress.py — ``NEXUS_PROGRESS`` records to queue progress.

``xodus-cli streaming --progress-json`` writes one record per line to stdout::

    NEXUS_PROGRESS {"schema":1,"event":"progress","sequence":9,
                    "phase":"downloading","paused":false,"canPause":true,
                    "completedBytes":0,"totalBytes":105408295,"bytesPerSecond":0}

The phases are ``initializing``, ``metadata``, ``downloading``,
``finalizing``, then ``complete`` or ``failed``. ``totalBytes`` is null
until ``downloading``. Stdout also carries the CLI's tracing lines, so a
record is found anywhere in a line, not only at its start. A stray fragment
in front of a record must not hide it. Parsing is ``cli_models.parse_progress_line``.

The mapper turns records into the dict every store hands the download queue
(``phase``, ``percentage``, ``downloaded_bytes``, ``total_bytes``,
``speed_bps``, ``eta_seconds``). It throttles to one update a second, but a
phase change or a terminal record is always sent at once.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from unifideck.stores.shared.cli_install_helpers import TailRingBuffer

from .cli_models import PROGRESS_MARK, ProgressRecord, parse_progress_line
from .diagnostics import redact

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]] | None

TERMINAL_PHASES = frozenset({"complete", "failed"})

#: The CLI's phase, as the download queue's ``download_phase``.
#:
#: ``initializing`` (credential and license checks) and ``metadata`` (reading
#: the package's file layout) come before the first byte, so they are
#: ``starting``. They are not ``preparing``: the frontend shows that phase as
#: the prefix setup and cloud-save sync other stores run after the download,
#: and nothing here builds a prefix. ``finalizing`` writes out the finished
#: package, which is not a file check, so it is not ``verifying``.
#:
#: A terminal record maps to None and sends no phase. The row then keeps the
#: phase of the step it ended in. On success the worker marks it complete,
#: and on failure the history shows which step died.
QUEUE_PHASE: dict[str, str | None] = {
    "initializing": "starting",
    "metadata": "starting",
    "downloading": "downloading",
    "finalizing": "finalizing",
    "complete": None,
    "failed": None,
}

#: Phases that print nothing for long stretches: credential checks before
#: the first byte, the package metadata fetch, and the final package write.
QUIET_PHASES = frozenset({"initializing", "metadata", "finalizing"})

#: Silence tolerated before the first record and while bytes move, in s.
STALL_S = 300.0
#: Silence tolerated in a quiet phase, in s. Not measured on a large game yet.
STALL_QUIET_S = 1800.0

#: The shortest gap between two progress updates of one phase.
THROTTLE_S = 1.0


def queue_payload(record: ProgressRecord) -> dict[str, Any]:
    """The download-queue progress dict for one record.

    The percentage, byte counts and ETA are sent only once ``totalBytes``
    is known. A total of 0 (an update with nothing to fetch) reads as 100%.
    A terminal record carries no ``phase`` (see ``QUEUE_PHASE``).
    """
    payload: dict[str, Any] = {"speed_bps": float(record.bytes_per_second)}
    phase = QUEUE_PHASE[record.phase]
    if phase is not None:
        payload["phase"] = phase
    total = record.total_bytes
    if total is None:
        return payload
    done = min(record.completed_bytes, total)
    payload["downloaded_bytes"] = done
    payload["total_bytes"] = total
    payload["percentage"] = 100.0 if total == 0 else round(done * 100.0 / total, 2)
    speed = record.bytes_per_second
    if speed > 0 and total > done:
        payload["eta_seconds"] = int((total - done) / speed)
    return payload


class ProgressMapper:
    """Reads one streaming run's stdout and feeds the download queue.

    One instance per run. :meth:`handle_line` is the line handler for
    ``run_drained_cli``: records become progress updates, every other line
    is redacted into the tail buffer for the failure message.
    """

    def __init__(
        self, tail: TailRingBuffer, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Keep non-record lines in ``tail``; ``clock`` drives the throttle."""
        self._tail = tail
        self._clock = clock
        self._sequence = -1
        self._phase: str | None = None
        self._last_sent = float("-inf")

    @property
    def phase(self) -> str | None:
        """The CLI phase of the last accepted record (None before the first)."""
        return self._phase

    @property
    def terminal(self) -> str | None:
        """``complete`` or ``failed`` once the run reported its end, else None."""
        return self._phase if self._phase in TERMINAL_PHASES else None

    def in_quiet_phase(self) -> bool:
        """Whether the long stall window applies to the next read.

        Before the first record the phase is None, so the short window
        applies: the CLI prints its first record within seconds of start.
        """
        return self._phase in QUIET_PHASES

    def feed(self, line: str) -> dict[str, Any] | None:
        """Accept the record in ``line`` and return the update to send, if any.

        A record whose sequence does not increase is a replay or a
        corruption, and is dropped, as is a phase this version does not know.
        """
        record = parse_progress_line(line, after=self._sequence)
        if record is None or record.phase not in QUEUE_PHASE:
            return None
        changed = record.phase != self._phase
        self._sequence = record.sequence
        self._phase = record.phase
        now = self._clock()
        if not changed and record.phase not in TERMINAL_PHASES:
            if now - self._last_sent < THROTTLE_S:
                return None
        self._last_sent = now
        return queue_payload(record)

    async def handle_line(
        self, line: str, game_id: str, progress_cb: ProgressCallback,
    ) -> None:
        """``run_drained_cli`` line handler for the streaming stdout."""
        if PROGRESS_MARK not in line:
            self._tail.append(redact(line)[:300])
            return
        update = self.feed(line)
        if update is None or progress_cb is None:
            return
        try:
            await progress_cb(update)
        except Exception:
            logger.exception("[xbox_install] progress callback failed for %s", game_id)
