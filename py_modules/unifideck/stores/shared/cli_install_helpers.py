from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    import re
    from collections.abc import Awaitable, Callable

#: The drain never calls the progress callback itself — it hands it back to
#: the store's own line handler untouched. Keeping it a type variable lets
#: each store declare the callback shape it actually uses (Epic accepts a
#: bare float *or* a dict, GOG and Amazon only a dict) instead of all three
#: being forced onto one union they don't share.
CallbackT = TypeVar("CallbackT")

#: Silence tolerated from a download CLI before its install is treated as
#: wedged, in seconds.
#:
#: One value for all of them because the reason is the same for all of them:
#: legendary, gogdl and nile each print a progress or heartbeat line roughly
#: once a second for the whole download — gogdl even at 0 MiB/s while it
#: retries the CDN — so two minutes of total silence is never a live transfer.
#:
#: This is the only bound that can fire during a download. A store's overall
#: install timeout runs through ``wait_with_timeout``, which is reached only
#: *after* EOF, so a CLI that wedges with stdout still open is invisible to
#: it. With ``DownloadService.max_concurrent`` defaulting to 1, that hung the
#: entire queue until the plugin was restarted (audit §3.2).
#:
#: A store that legitimately goes quiet at the end of a download pairs this
#: with ``finalize_s`` rather than raising this value — see GOG, which widens
#: to 30 minutes past 99% for archive extraction and the manifest write.
DEFAULT_STALL_TIMEOUT_S = 120.0

logger = logging.getLogger(__name__)


class InstallStalledError(RuntimeError):
    """A store CLI produced no output for longer than its stall window.

    Raised by :func:`drain_install_output`, never by the caller. The
    helper deliberately does **not** kill the process: every CLI store
    already owns a ``terminate_process_tree`` call for the cancel path,
    and routing the stall through the same arm keeps exactly one kill
    site per store instead of two that can drift apart.
    """

    def __init__(self, seconds: float, *, in_finalize: bool) -> None:
        """Record the window that elapsed and which phase it belonged to."""
        phase = "finalizing" if in_finalize else "downloading"
        super().__init__(
            f"stalled: no output for {int(seconds)}s while {phase}",
        )
        self.seconds = seconds
        self.in_finalize = in_finalize


class TailRingBuffer:
    """Bounded FIFO of a CLI's most-recent non-progress output lines.

    A store installer streams a download tool's stdout line-by-line and
    forwards only the progress/speed lines to the UI; the *other* lines
    (which is where the tool prints its actual error, e.g. legendary's
    ``[cli] ERROR: …``) would otherwise be dropped. Feed those lines to
    :meth:`append` and, on a non-zero exit, call :meth:`tail` to recover
    the last few for the failure message. One instance per install (bind
    it via ``functools.partial`` into the line handler) so concurrent or
    sequential installs never share state.
    """

    def __init__(self, maxlen: int = 20) -> None:
        """Keep at most ``maxlen`` lines (the newest win)."""
        self._lines: deque[str] = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        """Record one output line (empty lines are ignored)."""
        if line:
            self._lines.append(line)

    def tail(self, count: int = 5, sep: str = " | ") -> str:
        """Return the last ``count`` recorded lines joined by ``sep``."""
        if not self._lines:
            return ""
        recent = list(self._lines)[-count:]
        return sep.join(recent)


def join_tail(note: str, tail_buf: TailRingBuffer) -> str:
    """``note``, plus whatever the CLI last printed, as one error tail.

    Used when the failure is ours rather than the CLI's — a stall or a
    post-EOF hang — where the note names what we detected and the buffer
    still holds the last thing the tool said before it went quiet, which
    is usually the more useful half.
    """
    tail = tail_buf.tail()
    return f"{note} | {tail}" if tail else note
async def drain_install_output(
    proc: Any,
    game_id: str,
    progress_cb: CallbackT,
    line_handler: Callable[[str, str, CallbackT], Awaitable[None]],
    *,
    stall_s: float | None = None,
    finalize_s: float | None = None,
    in_finalize: Callable[[], bool] | None = None,
) -> None:
    """Stream a download CLI's stdout, one line at a time, until EOF.

    ``stall_s`` arms a **per-read** watchdog: if the CLI produces no
    output at all for that long, raise :class:`InstallStalledError`. Without
    it this awaits ``readline()`` forever, and the caller's overall
    install timeout is unreachable — ``wait_with_timeout`` only runs
    *after* EOF, so a CLI that wedges with stdout still open hangs the
    download queue until the plugin is restarted. All three CLI stores
    now arm it; GOG has since its own loop was written, Epic and Amazon
    never had any stall detection at all.

    ``finalize_s`` + ``in_finalize`` give the two-phase behaviour GOG
    proved in the field. Once the predicate returns True the window
    widens, because a download that has written every byte legitimately
    goes quiet through extraction, worker shutdown and the manifest
    write. A flat short window killed those installs mid-extraction; a
    flat long one cannot tell a stall from a slow CDN. Any line at all
    resets the window, and every one of these CLIs emits a ~1 Hz
    heartbeat while bytes are moving, which is what makes a short
    download-phase window safe.

    The predicate is polled before each read and may have side effects
    (GOG uses the transition to flip its row to "Extracting…").
    """
    assert proc.stdout is not None
    while True:
        window, tail = _read_window(stall_s, finalize_s, in_finalize)
        line_bytes = await _read_line(proc, window, in_finalize=tail)
        if not line_bytes:
            break
        line = line_bytes.decode(errors="ignore").strip()
        if line:
            await line_handler(line, game_id, progress_cb)


def _read_window(
    stall_s: float | None,
    finalize_s: float | None,
    in_finalize: Callable[[], bool] | None,
) -> tuple[float | None, bool]:
    """``(timeout, in_finalize)`` for the next read.

    A ``None`` timeout means wait indefinitely, which is what a caller
    that passes no ``stall_s`` gets. The predicate is polled here, once
    per read, so a caller can use the transition for its own side effect.
    """
    if stall_s is None:
        return None, False
    tail = (
        finalize_s is not None and in_finalize is not None and in_finalize()
    )
    return (finalize_s if tail else stall_s), tail


async def _read_line(
    proc: Any, window: float | None, *, in_finalize: bool,
) -> bytes:
    """One ``readline``, bounded by ``window``. Raises on a stall."""
    if window is None:
        # proc is Any (asyncio.subprocess.Process in production, a stub in
        # tests), so both reads come back untyped; the cast is the contract.
        return cast("bytes", await proc.stdout.readline())
    try:
        return cast(
            "bytes",
            await asyncio.wait_for(proc.stdout.readline(), timeout=window),
        )
    except TimeoutError:
        logger.warning(
            "[cli_install] no output for %ds (finalizing=%s) — stall",
            int(window), in_finalize,
        )
        raise InstallStalledError(window, in_finalize=in_finalize) from None
def _child_pids(pid: int) -> list[int]:
    """Direct children of ``pid``, from procfs. Empty if unreadable."""
    kids: list[int] = []
    task_dir = Path(f"/proc/{pid}/task")
    try:
        for tid in task_dir.iterdir():
            try:
                raw = (tid / "children").read_text()
            except OSError:
                continue
            kids.extend(int(p) for p in raw.split())
    except OSError:
        return []
    return kids


def _process_tree(pid: int) -> list[int]:
    """``pid`` plus every descendant, deepest last.

    Used to signal a download tool's whole tree. A process-group kill
    would be simpler but is NOT safe here: these children are spawned
    without a new session, so they share the plugin host's process group
    — ``killpg`` would take down ``plugin_loader`` itself.
    """
    seen: list[int] = [pid]
    frontier = [pid]
    while frontier:
        nxt: list[int] = []
        for parent in frontier:
            for kid in _child_pids(parent):
                if kid not in seen:
                    seen.append(kid)
                    nxt.append(kid)
        frontier = nxt
    return seen


async def terminate_process_tree(
    proc: Any, log_prefix: str, *, grace_s: float = 5.0,
) -> None:
    """Stop ``proc`` and every descendant; SIGKILL whatever survives.

    ``proc.kill()`` alone is not enough for legendary: it drives its
    downloads through ``multiprocessing`` workers, so killing just the
    parent leaves children alive — and they inherited the
    ``installed.json.lock`` file descriptor. A surviving child therefore
    keeps legendary's install lock held, and legendary answers *every*
    later install by printing a CRITICAL and **exiting 0** — which the
    caller then reads as success. That is exactly how a cancelled
    install turned every subsequent one into an instant phantom
    "success" with nothing on disk.

    The waits swallow ``CancelledError`` on purpose. The usual caller is
    a task that is *already* being cancelled, where every ``await``
    re-raises immediately — without this the SIGKILL escalation would be
    skipped and a SIGTERM-ignoring child would survive holding the lock,
    which is the whole failure being prevented. The signals themselves
    are sent before any ``await``, so they land regardless. Callers
    propagate their own cancellation afterwards.

    Safe to call on an already-exited process.
    """
    if proc.returncode is not None:
        return
    pids = _process_tree(proc.pid)
    _signal_tree(pids, signal.SIGTERM)
    await _settle(proc, grace_s)
    survivors = [pid for pid in pids if _pid_alive(pid)]
    if survivors:
        logger.warning(
            "%s %d process(es) ignored SIGTERM, sending SIGKILL: %s",
            log_prefix, len(survivors), survivors,
        )
        _signal_tree(survivors, signal.SIGKILL)
        await _settle(proc, grace_s)
    logger.info("%s terminated process tree %s", log_prefix, pids)


def _signal_tree(pids: list[int], sig: int) -> None:
    """Signal every pid, children before parents. Never raises."""
    for pid in reversed(pids):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


async def _settle(proc: Any, grace_s: float) -> None:
    """Give a signalled process a moment to exit; never raises."""
    with contextlib.suppress(
        TimeoutError, ProcessLookupError, asyncio.CancelledError,
    ):
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=grace_s)


def _pid_alive(pid: int) -> bool:
    """True while ``pid`` still exists (zombies count as gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # " (name) S rest" — state is the field after the closing paren.
    fields = stat.rpartition(")")[2].split()
    return bool(fields) and fields[0] != "Z"


async def wait_with_timeout(
    proc: Any,
    timeout_s: int,
    log_prefix: str,
) -> int:
    """Wait with timeout."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        logger.exception(
            "%s timeout after %ds, killing",
            log_prefix, timeout_s,
        )
        await terminate_process_tree(proc, log_prefix)
        return -1
    return proc.returncode or 0
def parse_percent_re(
    line: str, pattern: re.Pattern[str],
) -> float | None:
    """Pull a bare percentage out of ``line`` with a store's own regex.

    The *other* progress shape: legendary prints a percentage inside a
    free-form line and nile has a bracketed ``[ 42.5 % ]`` fallback, so
    each passes its own compiled pattern whose group 1 is the number.
    For the ``Progress: <pct> <written>/<total> … ETA:`` line that gogdl
    and nile share, use :func:`parse_transfer_progress` instead.

    Named for the regex rather than ``parse_progress_line``: Amazon used
    to import *two different functions of that name* — this one and its
    own store-local one, aliased at the import — which is a large part of
    why the duplication went unnoticed for so long (audit §3.2).
    """
    match = pattern.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
def parse_transfer_progress(line: str, progress: dict[str, Any]) -> bool:
    """Parse gogdl's / nile's shared progress line into ``progress``.

    Both print the same shape, which is why this is one function and not
    two::

        = Progress: 42.50 123456789/987654321, Running for: 00:01:30, ETA: 00:01:28

    Mutates ``progress`` in place and returns True when the line carried
    a usable update, so the caller knows whether to emit. Recognised keys:
    ``speed_bps``, ``progress_percent``, ``downloaded_bytes``,
    ``total_bytes``, ``eta_seconds``. Presentation (the localized phase
    label and
    friends) is deliberately left to the caller — that is the only thing
    the two store copies actually disagreed about.

    Replaces the near-verbatim copies in ``gog/install/progress.py`` and
    ``amazon/amazon_progress.py``. Those were kept apart on the grounds
    that the tokenising was "close but not byte-identical"; measured
    against 19 vectors of real legendary/gogdl/nile output they agree on
    every line, and diverge only on two shapes none of the three CLIs
    emits. See ``tests/unit/test_cli_progress_parsers_agree.py``, which
    pins that table so the claim stays checked rather than remembered.
    """
    speed_bps = parse_speed_bps(line)
    if speed_bps is not None:
        progress["speed_bps"] = speed_bps
    if "Progress:" not in line:
        return speed_bps is not None
    try:
        tokens = line.split("Progress:", 1)[1].strip().split()
        if len(tokens) < 2:
            return False
        progress["progress_percent"] = float(tokens[0])
        written, sep, total = tokens[1].rstrip(",").partition("/")
        if not sep:
            return True
        progress["downloaded_bytes"] = int(written)
        progress["total_bytes"] = int(total)
    except (ValueError, IndexError):
        return False
    eta = parse_eta_seconds(line)
    if eta is not None:
        progress["eta_seconds"] = eta
    return True
def parse_eta_seconds(line: str) -> int | None:
    """Parse ``ETA: HH:MM:SS`` (or ``MM:SS``) from a CLI line → seconds.

    Both legendary and gogdl print ``ETA: <clock>`` on their progress
    line. Returns ``None`` when no ETA token is present or it doesn't
    parse — the caller leaves the previous value in place.
    """
    if "ETA:" not in line:
        return None
    tail = line.split("ETA:", 1)[1].strip()
    if not tail:
        return None
    parts = tail.split()[0].split(":")
    try:
        if len(parts) == 3:
            h, m, s = (int(p) for p in parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = (int(p) for p in parts)
            return m * 60 + s
    except ValueError:
        return None
    return None
def parse_speed_bps(line: str) -> float | None:
    """Parse a ``+ Download … <n> MiB/s`` transfer-rate line → bytes/sec.

    Matches all three CLIs — gogdl (``+ Download\t+ 12.3 MiB/s``),
    legendary and nile (``+ Download\t- 12.3 MiB/s``). The sign is
    normally its own token, so the rate is the last token before
    ``MiB/s``. The ``Download`` guard skips legendary's
    ``+ Disk … MiB/s`` and ``Downloaded: … MiB`` lines (the latter has
    no ``/s``). Returns ``None`` on no match.

    Splitting after ``Download`` rather than from the start of the line,
    and stripping a leading ``-``, are both taken from the Amazon copy
    this replaces: it was the only one of the three that could not
    return a *negative* transfer rate for an unspaced ``-9.75 MiB/s``,
    which would have rendered as a negative MB/s in the download row.
    """
    if "Download" not in line or "MiB/s" not in line:
        return None
    tail = line.split("Download", 1)[1]
    tokens = tail.split("MiB/s", 1)[0].strip().lstrip("-").split()
    if not tokens:
        return None
    try:
        return float(tokens[-1]) * 1024 * 1024
    except ValueError:
        return None
