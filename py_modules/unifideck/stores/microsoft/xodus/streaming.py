"""stores/microsoft/xodus/streaming.py — supervise one ``xodus-cli streaming`` run.

The process is started by ``XodusCli.spawn_streaming`` (the only spawn site)
with stdout and stderr on separate pipes. This module drains both at once:

* stdout through the shared ``run_drained_cli`` envelope, which feeds the
  progress records to :class:`~.progress.ProgressMapper` and kills the
  process tree on a stall, a cancel or any other exception;
* stderr through :func:`~.diagnostics.drain_stderr`, for the diagnostics.

Stall windows: 300 s before the first record and while bytes move, and
1800 s in the quiet phases (credential checks, package metadata, the final
package write). Any stdout line resets the window.

There is no cancel command. Cancel kills the tree; the CLI resumes from its
own journal (``.nexus-streaming-v1.jsonl``) on the next run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from unifideck.stores.shared.cli_install_helpers import DrainedRun, TailRingBuffer, run_drained_cli

from .diagnostics import Diagnostics, drain_stderr, stop_drain
from .progress import STALL_QUIET_S, STALL_S, ProgressCallback, ProgressMapper

LOG_PREFIX = "[xbox_install]"

#: How long the process may take to exit after stdout closed, in s. A run
#: that reported ``complete`` and then hangs is killed after this, and the
#: retry resumes almost at once.
EXIT_WAIT_S = 120


@dataclass(frozen=True)
class StreamOutcome:
    """How a streaming run ended.

    ``terminal`` is ``complete`` or ``failed`` when the CLI reported its end,
    else None. ``rc`` is -1 after a stall or a hung exit.
    """

    rc: int
    terminal: str | None
    tail: str
    diagnostics: Diagnostics
    stalled: bool = False

    @property
    def succeeded(self) -> bool:
        """The CLI reported ``complete`` and exited 0."""
        return self.rc == 0 and self.terminal == "complete"


async def run_streaming(
    proc: Any, pid: str, progress_cb: ProgressCallback,
) -> StreamOutcome:
    """Drain ``proc`` to its end and say how it went.

    A cancel kills the process tree, then re-raises.
    """
    tail = TailRingBuffer(maxlen=40)
    diags = Diagnostics()
    mapper = ProgressMapper(tail)
    stderr_task: asyncio.Task[None] = asyncio.ensure_future(
        drain_stderr(proc.stderr, tail, diags),
    )
    try:
        drained = await run_drained_cli(
            proc, pid, progress_cb, mapper.handle_line, tail,
            log_prefix=LOG_PREFIX, wait_s=EXIT_WAIT_S,
            stall_s=STALL_S, finalize_s=STALL_QUIET_S,
            in_finalize=mapper.in_quiet_phase,
        )
    except (ValueError, asyncio.LimitOverrunError) as e:
        # Most often a stdout line longer than the 1 MiB spawn limit. The
        # tree is already killed. The run fails with its tail like any
        # other failed exit, so the user sees a coded reason.
        tail.append(f"unreadable stdout: {type(e).__name__}")
        drained = DrainedRun(rc=-1, tail=tail.tail())
    finally:
        await stop_drain(stderr_task)
    return StreamOutcome(
        rc=drained.rc,
        terminal=mapper.terminal,
        tail=drained.tail if drained.stalled else tail.tail(),
        diagnostics=diags,
        stalled=drained.stalled,
    )
