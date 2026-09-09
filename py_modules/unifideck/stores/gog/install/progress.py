"""gogdl subprocess + progress monitor.

``_GogdlProgressMonitor`` wraps the ``gogdl`` subprocess invocation
with structured progress reporting:

* parses gogdl's stdout/stderr stream to extract download progress
  (percentage, transfer rate, ETA) — via the shared parsers in
  ``stores/shared/cli_install_helpers``, which all three CLI stores use;
* enforces a two-phase stall watchdog — if gogdl stops producing output
  for too long, kill its process tree and report failure;
* captures gogdl's non-progress output so a failed install can say what
  actually went wrong instead of a bare error code.

The conditional ``gogdl repair`` pass lives in ``repair.py`` and is mixed
into the monitor, so ``installer.py`` still reaches it through one object.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
)

from unifideck.stores.shared.cli_install_helpers import (
    DEFAULT_STALL_TIMEOUT_S,
    InstallStalledError,
    TailRingBuffer,
    drain_install_output,
    join_tail,
    parse_transfer_progress,
    terminate_process_tree,
)

from .repair import _GogdlRepairMixin

if TYPE_CHECKING:
    from .installer import GOGInstaller
logger = logging.getLogger(__name__)
# Stall watchdog for the *active download* phase — the shared default, since
# gogdl's ~1 Hz "+ Disk … (write)/(read)" heartbeat behaves like the other two
# CLIs'. The finalize window below is what makes GOG's watchdog two-phase.
_GOGDL_STALL_TIMEOUT_S = DEFAULT_STALL_TIMEOUT_S
# Tolerated silence once bytes are complete (~100%): gogdl may go quiet during
# native archive extraction / worker shutdown / manifest write before EOF, and
# the bounded post-EOF wait covers a process that closes stdout then lingers.
# NOTE: no absolute wall-clock cap on the tail — a legitimately slow CDN can
# keep a download at ~100% for a long time, and killing that would fail a
# working install. The per-read window is the only bound; the heartbeat keeps
# it from firing on a live-but-slow download.
_GOGDL_FINALIZE_TIMEOUT_S = 1800.0
# Progress threshold at which the download is effectively done and we flip the
# UI to the indeterminate "Extracting…" phase so the row stops looking frozen.
_GOGDL_TAIL_PROGRESS_PCT = 99.0
# Synthetic exit codes for failures that are ours, not gogdl's: it never
# started, or it went quiet / would not exit and we killed it.
_RC_SPAWN_FAILED = -2
_RC_KILLED = -1

@dataclass
class _RunOutcome:
    """Result of one ``gogdl`` subprocess run.

    ``tail`` carries gogdl's last non-progress output lines so a failed
    install can name its real cause — a full disk, a dropped connection,
    an expired token — instead of the bare ``download_failed`` code this
    used to return as a plain ``bool``. Epic and Amazon have carried the
    equivalent for releases; GOG never did, and because
    ``friendlyDownloadError`` classifies off the *message text*, a GOG
    user got an untranslated machine token where the other two got a
    localized explanation (audit §3.2).

    ``rc`` is gogdl's exit code, ``-1`` for a stall or a post-EOF hang and
    ``-2`` when the process could not be spawned at all.
    """

    rc: int
    tail: str = ""

    @property
    def ok(self) -> bool:
        """True when gogdl completed successfully."""
        return self.rc == 0

@dataclass
class _RunState:
    """Per-run mutable state shared between the drain and its line handler."""

    progress: dict[str, Any] = field(default_factory=lambda: {
        "progress_percent": 0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "speed_bps": 0.0,
        "eta_seconds": 0,
    })
    tail_buf: TailRingBuffer = field(default_factory=TailRingBuffer)
    in_tail: bool = False

def format_gogdl_error(outcome: _RunOutcome) -> str:
    """``gogdl_exit_{rc}: {tail}`` — the house error format.

    Matches ``epic/install._format_exit_error`` and its Amazon twin, and
    the ``EXIT_PREFIX_RE`` in ``src/lib/download-errors.ts`` already
    strips exactly this prefix — the frontend has anticipated a
    ``gogdl_exit_N:`` for a long time while nothing ever emitted one.
    Keeping the prefix machine-parsable while carrying the tail is what
    lets the shared classifier map a full disk or a dead connection to a
    translated string, with no new locale keys.
    """
    if outcome.rc == _RC_SPAWN_FAILED:
        # Matches Amazon's ``nile_spawn_failed``: a process that never
        # started has no exit code to report, and the frontend maps the
        # ``_spawn_failed`` suffix to "Required download tool not found",
        # which is the truth here — retrying will not help a lost exec bit.
        return f"gogdl_spawn_failed: {outcome.tail}"
    base = f"gogdl_exit_{outcome.rc}"
    return f"{base}: {outcome.tail}" if outcome.tail else base

class _GogdlProgressMonitor(_GogdlRepairMixin):
    """Gogdl progress monitor (download half; repair half is the mixin)."""

    def __init__(self, parent: GOGInstaller) -> None:
        """Initialize the instance."""
        self._parent = parent

    async def run_gogdl_with_progress(
        self,
        install_mode: str,
        game_id: str,
        platform: str,
        path: str,
        support_dir: str,
        languages: list[str],
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> _RunOutcome:
        """Run GOGDL with progress."""
        env, creds_path, cleanup = await self._parent._tokens.acquire_gogdl_creds()
        try:
            cmd = self._build_gogdl_cmd(
                creds_path,
                install_mode,
                game_id,
                platform,
                path,
                support_dir,
                languages,
            )
            try:
                proc = await self._spawn_gogdl(cmd, env)
            except OSError as e:
                # A lost exec bit or a missing loader on bin/gogdl would
                # otherwise escape as a raw OSError string in the download
                # row. Amazon has always reported this properly.
                logger.exception("[GOGInstaller] cannot spawn %s", cmd[0])
                return _RunOutcome(rc=_RC_SPAWN_FAILED, tail=str(e))
            return await self._run_and_watch(proc, game_id, progress_cb)
        finally:
            await cleanup()

    async def _run_and_watch(
        self,
        proc: asyncio.subprocess.Process,
        game_id: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> _RunOutcome:
        """Drain gogdl, killing its whole tree on stall or cancellation."""
        state = _RunState()
        stalled: InstallStalledError | None = None
        drain_exc: BaseException | None = None
        try:
            await self._read_progress_loop(proc, state, progress_cb)
        except InstallStalledError as e:
            stalled = e
        except BaseException as e:
            drain_exc = e
        if stalled is not None or drain_exc is not None:
            # Unwinding this coroutine does not stop gogdl: it drives its
            # downloads through multiprocessing workers, so killing only
            # the parent leaves children writing into the game directory
            # after the row already reads "Cancelled" — the same failure
            # legendary had before ``terminate_process_tree`` existed.
            await terminate_process_tree(proc, "[GOGInstaller]")
        if drain_exc is not None:
            raise drain_exc
        if stalled is not None:
            return _RunOutcome(
                rc=_RC_KILLED, tail=join_tail(str(stalled), state.tail_buf),
            )
        return await self._await_exit(proc, state)

    async def _await_exit(
        self,
        proc: asyncio.subprocess.Process,
        state: _RunState,
    ) -> _RunOutcome:
        """Wait for a process that has closed stdout, then read its code.

        Bounded so a gogdl that closes stdout and then hangs cannot wedge
        the queue forever; the read loop already tolerated the silent
        extraction tail via the finalize window.
        """
        try:
            await asyncio.wait_for(proc.wait(), timeout=_GOGDL_FINALIZE_TIMEOUT_S)
        except TimeoutError:
            logger.warning(
                "[GOGInstaller] gogdl did not exit after EOF (%ds) — killing",
                int(_GOGDL_FINALIZE_TIMEOUT_S),
            )
            await terminate_process_tree(proc, "[GOGInstaller]")
            return _RunOutcome(
                rc=_RC_KILLED,
                tail=join_tail("did not exit after closing output", state.tail_buf),
            )
        rc = proc.returncode or 0
        if rc != 0:
            logger.error("[GOGInstaller] gogdl exited with code %d", rc)
        return _RunOutcome(rc=rc, tail=state.tail_buf.tail())

    def _build_gogdl_cmd(
        self,
        creds_path: str,
        install_mode: str,
        game_id: str,
        platform: str,
        path: str,
        support_dir: str,
        languages: list[str],
    ) -> list[str]:
        """Build GOGDL cmd."""
        cmd = [
            self._parent._gogdl_bin,
            "--auth-config-path",
            creds_path,
            install_mode,
            game_id,
            "--platform",
            platform,
            "--path",
            path,
            "--support",
            support_dir,
            "--with-dlcs",
        ]
        for lang in languages:
            cmd.extend(["--lang", lang])
        return cmd

    async def _spawn_gogdl(
        self,
        cmd: list[str],
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        """Spawn GOGDL."""
        logger.info(
            "[GOGInstaller] spawning gogdl: %s",
            " ".join(cmd),
        )
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

    async def _read_progress_loop(
        self,
        proc: asyncio.subprocess.Process,
        state: _RunState,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Drain gogdl's output under the two-phase stall watchdog.

        While bytes are still downloading a stall is a hard failure (tight
        ``_GOGDL_STALL_TIMEOUT_S``). Once progress crosses
        ``_GOGDL_TAIL_PROGRESS_PCT`` the download is effectively done; gogdl can
        go quiet during finalization, so the per-read window widens to
        ``_GOGDL_FINALIZE_TIMEOUT_S`` and the UI flips to the indeterminate
        "Extracting…" phase so the row stops looking frozen at 100%. Any line
        (including gogdl's ~1 Hz heartbeat) resets the window, so a live-but-slow
        download is never killed.

        The loop itself now lives in ``shared/cli_install_helpers``: this shape
        was GOG-only, and Epic and Amazon had no stall detection at all — their
        install timeouts sit *after* EOF and so cannot fire while a wedged CLI
        holds stdout open. Raising rather than killing keeps a single
        ``terminate_process_tree`` site per store (see ``_run_and_watch``).
        """
        await drain_install_output(
            proc,
            "",  # game_id is unused by this handler; state carries the run
            progress_cb,
            functools.partial(self._handle_progress_line, state=state),
            stall_s=_GOGDL_STALL_TIMEOUT_S,
            finalize_s=_GOGDL_FINALIZE_TIMEOUT_S,
            in_finalize=lambda: state.in_tail,
        )

    async def _maybe_enter_tail(
        self,
        state: _RunState,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Flip into the finalization tail once download bytes are complete.

        Emits a single ``phase="extracting"`` callback so the UI switches to
        the indeterminate "Extracting…" spinner. Idempotent — a no-op once
        already in the tail, so the spinner never flips back.
        """
        if state.in_tail:
            return
        pct = float(state.progress.get("progress_percent") or 0)
        if pct < _GOGDL_TAIL_PROGRESS_PCT:
            return
        state.in_tail = True
        # Stamp the shared dict so the phase stays "extracting" for any later
        # callbacks too — the indeterminate spinner shouldn't flip back.
        state.progress["phase"] = "extracting"
        if progress_cb is not None:
            try:
                await progress_cb(
                    dict(state.progress),
                )
            except Exception as e:
                logger.debug("[GOGInstaller] extracting phase_cb: %s", e)
        logger.info("[GOGInstaller] download bytes complete → extracting/finalizing")

    async def _handle_progress_line(
        self,
        line_str: str,
        _game_id: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
        *,
        state: _RunState,
    ) -> None:
        """Route one gogdl output line: progress to the UI, the rest to the tail."""
        is_progress_line = "Progress:" in line_str or "Download" in line_str
        if not is_progress_line:
            # Where gogdl prints its actual error. Captured so a failed
            # install can report the cause rather than a bare exit code.
            state.tail_buf.append(line_str)
            if not line_str.startswith("[gogdl]"):
                logger.info("[gogdl] %s", line_str)
        # ``parse_transfer_progress`` mutates ``state.progress`` in place;
        # the ``if updated and "Progress:" in line_str`` branch that used to
        # follow existed solely to format a ``phase_message`` string the UI
        # never displayed (audit register item 45), so it went with it.
        updated = parse_transfer_progress(line_str, state.progress)
        await self._maybe_enter_tail(state, progress_cb)
        if progress_cb is None or not updated:
            return
        try:
            await progress_cb(dict(state.progress))
        except Exception as e:
            logger.debug(
                "[GOGInstaller] progress_cb: %s",
                e,
            )
