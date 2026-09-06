"""Epic Games installer — install/uninstall pipeline using legendary.

``EpicInstaller`` orchestrates installs through ``legendary`` :

1. **preflight** — verify legendary binary, resolve base path, build
   the install context;
2. **probe** — call ``legendary info`` for the game's manifest
   (size, supported languages, executable path);
3. **subprocess** — spawn legendary with structured progress callbacks
   (parses legendary's stdout for "+ Downloaded: X/Y" lines);
4. **finalize** — resolve the launchable .exe (delegate to
   ``exe_resolver.py``), write the ``.unifideck-id`` marker,
   register with the install registry, regenerate manifest.

The uninstall path is symmetric : remove install dir, drop registry
entry, clean up shortcut + artwork cache, and run ``legendary
uninstall`` to keep legendary's bookkeeping in sync.

Errors at any phase are wrapped into typed ``InstallResult``
envelopes ; partial installs are cleaned up to avoid leaving
orphaned files on disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.core.manifest import write_manifest
from unifideck.core.types import Events, InstallResult, Result
from unifideck.event_bus.event_bus import EventBus
from unifideck.stores.shared import dlc
from unifideck.stores.shared.cli_install_helpers import (
    DEFAULT_STALL_TIMEOUT_S,
    InstallStalledError,
    TailRingBuffer,
    drain_install_output,
    join_tail,
    parse_eta_seconds,
    parse_percent_re,
    parse_speed_bps,
    terminate_process_tree,
    wait_with_timeout,
)

# Imported as modules, not names: ``uninstall_game``'s own
# ``delete_prefix`` parameter would otherwise shadow the function.
from . import sdl, uninstall
from .exe_resolver import EpicExeResolver
from .legendary import write_app_language
from .library import EpicLibraryReader

logger = logging.getLogger(__name__)

_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# The worker's progress callback accepts either a bare percentage or a
# partial dict (``percentage`` / ``speed_bps`` / ``eta_seconds`` / …)
# and merges dict updates onto the queue item — see DownloadWorker.
ProgressCallback = Callable[[float | dict[str, Any]], Awaitable[None]]

@dataclass
class _RunOutcome:
    """Result of one ``legendary install`` subprocess run.

    ``rc`` is the exit code (``-1`` on timeout); ``tail`` is the last few
    non-progress output lines captured by the ring buffer — legendary's
    real error text when ``rc != 0``, so the failure isn't reduced to a
    bare ``legendary_exit_{rc}``.
    """

    rc: int
    tail: str = ""

def _format_exit_error(outcome: _RunOutcome) -> str:
    """Turn a failed run into an error string.

    Keeps the machine-parsable ``legendary_exit_{rc}`` prefix (matched by
    downstream classification / callers) and appends legendary's actual
    output tail when captured, so logs, the tracker, and the download
    item's ``error_message`` name the real reason instead of a bare code.
    """
    base = f"legendary_exit_{outcome.rc}"
    return f"{base}: {outcome.tail}" if outcome.tail else base

# How legendary dies when its Selective Downloads prompt can't read
# stdin (``sdl_prompt`` → bare ``input()`` → EOFError). Retrying is
# pointless — attempt two hits the same prompt — so the DLC fallback
# must not burn a whole second download on it.
_PROMPT_CRASH_MARKERS = ("EOFError", "sdl_prompt")

def _is_prompt_crash(tail: str) -> bool:
    """True when legendary died on an unanswerable interactive prompt."""
    return any(marker in tail for marker in _PROMPT_CRASH_MARKERS)

# legendary guards install/import/move with a FileLock on
# ``installed.json.lock`` and allows exactly one at a time. When it can't
# take the lock it logs this at CRITICAL and then **exits 0**, so the
# refusal is indistinguishable from success by exit code alone.
_LOCK_REFUSAL_MARKER = "Failed to acquire installed data lock"

def _no_install_error(outcome: _RunOutcome) -> str:
    """Name a ``rc == 0`` run that installed nothing.

    Keeps a machine-parsable prefix, like :func:`_format_exit_error`.
    """
    if _LOCK_REFUSAL_MARKER in outcome.tail:
        return (
            "legendary_install_lock_busy: another Epic install is still "
            f"holding legendary's install lock — {outcome.tail}"
        )
    return f"legendary_exit_0_no_install: {outcome.tail or '(no output captured)'}"

class EpicInstaller:
    """Epic installer."""

    def __init__(
        self,
        bus: EventBus,
        cli_path: str | None,
        library: EpicLibraryReader,
        exe_resolver: EpicExeResolver,
        default_install_root: str,
        install_timeout_seconds: int = 7200,
        uninstall_timeout_seconds: int = 120,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._cli_path = cli_path
        self._library = library
        self._exe_resolver = exe_resolver
        self._default_install_root = str(Path(default_install_root).expanduser())
        self._install_timeout = install_timeout_seconds
        self._uninstall_timeout = uninstall_timeout_seconds

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
        language: str | None = None,
    ) -> InstallResult:
        """Install game.

        ``language`` is the user's install-language choice for a
        Selective Downloads title (one of the option keys the picker
        offered, or a bare locale tag when nothing was picked). It only
        affects which optional language pack is downloaded — see
        :mod:`unifideck.stores.epic.sdl`.
        """
        logger.info("[EpicInstall] install_game game_id=%s base_path=%s cli_path=%s",
                     game_id, base_path, self._cli_path)
        if not self._cli_path:
            logger.error("[EpicInstall] legendary CLI not found at %s", self._cli_path)
            return InstallResult(
                success=False,
                error="legendary_not_found",
                store="epic",
                game_id=game_id,
            )
        base = base_path or self._default_install_root
        mkdir_error = await self._prepare_base_dir(base)
        if mkdir_error:
            return InstallResult(
                success=False,
                error=mkdir_error,
                store="epic",
                game_id=game_id,
            )
        outcome = await self._run_install_with_dlc_fallback(
            base, game_id, progress_cb, language,
        )
        logger.info("[EpicInstall] legendary exit_code=%d", outcome.rc)
        if outcome.rc != 0:
            return self._fail(game_id, _format_exit_error(outcome))
        if not await self._install_was_recorded(game_id, outcome):
            return self._fail(game_id, _no_install_error(outcome))
        return await self._complete_install(game_id, base, language)

    async def _prepare_base_dir(self, base: str) -> str | None:
        """Create the install root. Returns an error string on failure."""
        try:
            await asyncio.to_thread(
                lambda: Path(base).mkdir(parents=True, exist_ok=True),
            )
        except OSError as e:
            logger.exception("[EpicInstall] mkdir failed: %s", base)
            return f"mkdir_failed: {e}"
        logger.info("[EpicInstall] install root ready: %s", base)
        return None

    async def _complete_install(
        self, game_id: str, base: str, language: str | None,
    ) -> InstallResult:
        """Post-install bookkeeping, then resolve the exe and register."""
        self._library.invalidate_installed_cache()
        if language:
            # Remember the per-game choice so the launcher's -epiclocale
            # matches the language pack we just downloaded, instead of
            # falling back to the global Unifideck language.
            await asyncio.to_thread(write_app_language, game_id, language)
        return await self._finalize_install(game_id, base)

    def _fail(self, game_id: str, error: str) -> InstallResult:
        """Wrap a terminal error in a failing ``InstallResult``.

        Emits nothing. ``DownloadWorker`` is the sole emitter of every
        ``DOWNLOAD_*`` event for all seven stores: it turns this envelope
        into the one ``DOWNLOAD_FAILED`` (``_emit_failure``), and only its
        payload carries the queue item the UI needs — the game title for
        the toast and ``error_message`` for the history row. This method
        used to emit its own store-shaped copy, which reached the frontend
        as a second, title-less failure toast.
        """
        return InstallResult(
            success=False,
            error=error,
            store="epic",
            game_id=game_id,
        )

    async def _install_was_recorded(
        self, game_id: str, outcome: _RunOutcome,
    ) -> bool:
        """True when legendary actually installed the game.

        Exit 0 is NOT proof. legendary answers a refusal — most notably
        "another instance holds the install lock" — with a CRITICAL log
        line and then **exit 0**. That used to be reported as success:
        the shortcut flipped to installed with an empty exe_path and
        nothing on disk, so the download appeared to finish instantly.
        legendary writes ``installed.json`` before exiting, so its own
        bookkeeping is the only trustworthy signal.
        """
        recorded = await asyncio.to_thread(
            uninstall.read_legendary_install_path, game_id,
        )
        if recorded:
            return True
        logger.error(
            "[EpicInstall] %s: legendary exited 0 but recorded no install — "
            "treating as failure. Output: %s",
            game_id, outcome.tail or "(none captured)",
        )
        return False

    async def _run_install_with_dlc_fallback(
        self, base: str, game_id: str, progress_cb: ProgressCallback | None,
        language: str | None = None,
    ) -> _RunOutcome:
        """Install with DLC, then retry the base game alone on failure.

        legendary installs the base game first and *then* each DLC
        (``--with-dlcs``); a single DLC with a broken/withheld manifest
        aborts the whole command non-zero even though the base game is
        already downloaded. So when the DLC-inclusive attempt fails on a
        DLC-capable store, retry once with ``--skip-dlcs`` (an explicit
        skip — with ``--yes`` legendary would otherwise still auto-install
        DLC) to recover the playable base game. A recovered install must
        not look like a failure to the user: the first attempt reports
        nothing, and only the ``InstallResult`` ``install_game`` finally
        returns decides whether ``DownloadWorker`` emits
        ``DOWNLOAD_FAILED``.

        Install tags are resolved once and reused across both attempts —
        the retry differs only in its DLC flag.
        """
        tags = await sdl.resolve_install_tags(game_id, language)
        with_dlc = dlc.store_supports_dlc("epic")
        outcome = await self._run_install(base, game_id, with_dlc, progress_cb, tags)
        if outcome.rc == 0 or not with_dlc:
            return outcome
        if _is_prompt_crash(outcome.tail):
            logger.error(
                "[EpicInstall] %s died on an interactive prompt: %s — not "
                "retrying (the retry would hit the same prompt)",
                game_id, outcome.tail or "(no output captured)",
            )
            return outcome
        logger.warning(
            "[EpicInstall] DLC install of %s failed (exit %d): %s — "
            "retrying base game without DLC",
            game_id, outcome.rc, outcome.tail or "(no output captured)",
        )
        return await self._run_install(base, game_id, False, progress_cb, tags)

    async def _run_install(
        self, base: str, game_id: str, with_dlc: bool,
        progress_cb: ProgressCallback | None = None,
        install_tags: list[str] | None = None,
    ) -> _RunOutcome:
        """Run one ``legendary install``, capturing its output tail."""
        cmd = self._build_install_cmd(base, game_id, with_dlc, install_tags)
        logger.info("[EpicInstall] executing: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_cli_env(),
        )
        tail_buf = TailRingBuffer()
        stalled: InstallStalledError | None = None
        drain_exc: BaseException | None = None
        try:
            await self._drain_install_output(proc, game_id, progress_cb, tail_buf)
        except InstallStalledError as e:
            stalled = e
        except BaseException as e:
            drain_exc = e
        if stalled is not None or drain_exc is not None:
            # Cancelling the download task only unwinds *our* coroutine —
            # legendary keeps running, and its multiprocessing children
            # keep legendary's install lock held, which makes every later
            # install exit 0 without installing. Kill the tree before
            # propagating, or a cancel poisons the whole queue. A stall
            # takes the same exit: the queue is serial, so a wedged
            # legendary blocks every game behind it until it is killed.
            await terminate_process_tree(proc, "[epic_install]")
        if drain_exc is not None:
            raise drain_exc
        if stalled is not None:
            return _RunOutcome(rc=-1, tail=join_tail(str(stalled), tail_buf))
        rc = await self._wait_with_timeout(proc)
        return _RunOutcome(rc=rc, tail=tail_buf.tail())

    def _build_install_cmd(
        self, base: str, game_id: str, with_dlc: bool,
        install_tags: list[str] | None = None,
    ) -> list[str]:
        """Build install cmd (DLC flag + Selective Downloads answer).

        ``--skip-sdl`` goes on unconditionally: it is inert for a
        non-SDL title, and it is the belt that keeps legendary from ever
        reaching its unanswerable ``sdl_prompt`` (the UD-026 install
        failure) should our tag resolution have come up empty.

        When tags *were* resolved they are passed as explicit
        ``--install-tag`` values, which also suppresses the prompt
        (legendary's ``sdl_enabled`` is false whenever ``install_tag``
        is set) while downloading the packs we actually chose.
        """
        if self._cli_path is None:
            raise RuntimeError("legendary CLI path is not set; cannot build install cmd")
        cmd = [
            self._cli_path,
            "install",
            game_id,
            "--base-path",
            base,
            "--yes",
            "--skip-sdl",
        ]
        for tag in install_tags or []:
            cmd.extend(["--install-tag", tag])
        cmd.extend(dlc.get_dlc_flags("epic", with_dlc))
        return cmd

    async def _drain_install_output(
        self, proc: Any, game_id: str,
        progress_cb: ProgressCallback | None, tail_buf: TailRingBuffer,
    ) -> None:
        """Drain install output, feeding non-progress lines to ``tail_buf``."""
        await drain_install_output(
            proc,
            game_id,
            progress_cb,
            functools.partial(self._handle_install_line, tail_buf=tail_buf),
            stall_s=DEFAULT_STALL_TIMEOUT_S,
        )

    async def _wait_with_timeout(self, proc: Any) -> int:
        """Wait with timeout."""
        return await wait_with_timeout(
            proc,
            self._install_timeout,
            "[epic_install]",
        )

    async def _handle_install_line(
        self, line: str, game_id: str, progress_cb: ProgressCallback | None,
        *, tail_buf: TailRingBuffer,
    ) -> None:
        """Parse one line of legendary's install output.

        legendary's DLManager spreads a single progress tick across
        several lines, e.g.::

            Progress: 50.5% (1234/2444), Running for 00:01:30, ETA: 00:01:28
             + Download	- 15.50 MiB/s (raw) / 14.00 MiB/s (decompressed)

        The percentage + ETA live on the ``Progress:`` line and the
        transfer rate on the ``Download`` line, so each line emits a
        *partial* update; the download worker merges them onto the
        queue item (keeping the last value for fields a line omits).
        Earlier only the percentage was parsed, so the UI was stuck at
        ``0.0 MB/s · ETA --:--`` for the whole install.

        Non-progress lines (where legendary prints its ``[cli] ERROR: …``)
        are also fed to ``tail_buf`` so a failing install can surface the
        real reason instead of a bare exit code.
        """
        # Transfer-rate line — forward speed only.
        speed_bps = parse_speed_bps(line)
        if speed_bps is not None:
            await self._safe_progress(progress_cb, {"speed_bps": speed_bps})
            return
        if "Progress:" not in line:
            tail_buf.append(line)
            logger.debug("[legendary install] %s", line)
            return
        pct = parse_percent_re(line, _PROGRESS_RE)
        if pct is None:
            return
        update: dict[str, Any] = {"percentage": pct}
        eta = parse_eta_seconds(line)
        if eta is not None:
            update["eta_seconds"] = eta
        await self._safe_progress(progress_cb, update)

    async def _safe_progress(
        self, progress_cb: ProgressCallback | None, update: dict[str, Any],
    ) -> None:
        """Invoke ``progress_cb`` with a partial update, swallowing errors."""
        if progress_cb is None:
            return
        try:
            await progress_cb(update)
        except Exception as e:
            logger.debug("[epic_install] progress_cb raised: %s", e)

    async def _finalize_install(self, game_id: str, base: str) -> InstallResult:
        """Finalize install."""
        resolved = await self._exe_resolver.resolve(game_id)
        install_path = resolved["install_path"]
        exe = resolved["executable"]
        title = resolved["title"]
        if install_path:
            exe_relative = ""
            if exe:
                with contextlib.suppress(ValueError):
                    # ``os.path.relpath`` is pure string manipulation —
                    # no filesystem access — so the ASYNC240 rule
                    # gives a false positive here.
                    exe_relative = os.path.relpath(  # noqa: ASYNC240 — pure string op, no I/O
                        exe,
                        install_path,
                    )
            await write_manifest(
                install_dir=install_path,
                store="epic",
                store_id=game_id,
                title=title,
                executable_relative=exe_relative,
                platform="windows",
            )
        # No ``DOWNLOAD_COMPLETE`` here. The install is not finished at this
        # point: ``DownloadWorker`` still has to run prefix warmup (up to
        # 600s of createprefix + winetricks + cloud-save pull) while the row
        # sits in its "preparing" phase, and it emits the one completion
        # afterwards, carrying the ``Game`` record that flips the shortcut's
        # install tag. Emitting from here announced the install as done
        # minutes early — the same premature-completion trap that broke
        # Battle.net installs (see launcher/wrapper_prefix_probe.py).
        return InstallResult(
            success=True,
            store="epic",
            game_id=game_id,
            install_path=install_path
            or str(Path(base) / game_id),
        )

    async def uninstall_game(
        self, game_id: str, delete_prefix: bool = False,
    ) -> Result:
        """Remove a game's files and clean up legendary's bookkeeping.

        ``legendary uninstall`` cannot be trusted to delete files: its
        per-game catalog lookup can fail with HTTP 401 (expired Epic
        auth), after which it **skips the deletion but still exits 0**,
        printing "please remove <path> manually". The old code only
        checked the exit code, so it reported success while leaving the
        full install (often many GiB) on disk.

        So we resolve the install dir from legendary's *local*
        ``installed.json`` (no network), run ``legendary uninstall`` only
        as best-effort metadata cleanup, then delete the directory and
        purge the registry entry ourselves — the latter is essential
        because a leftover entry makes the next library sync re-flag the
        game installed.

        The mechanics live in :mod:`unifideck.stores.epic.uninstall`;
        this method owns the ordering and what gets reported.
        """
        # Resolve the install dir while legendary's bookkeeping is intact.
        install_path = await asyncio.to_thread(
            uninstall.read_legendary_install_path, game_id,
        )

        if self._cli_path:
            await uninstall.best_effort_legendary_uninstall(
                self._cli_path, game_id, self._uninstall_timeout,
            )

        removed = await uninstall.delete_install_dir(install_path, game_id)

        # legendary leaves the installed.json row behind when it 401s;
        # drop it so the next sync doesn't resurrect the game as installed.
        await asyncio.to_thread(uninstall.purge_legendary_install_entry, game_id)

        if delete_prefix:
            await uninstall.delete_prefix(game_id)

        self._library.invalidate_installed_cache()
        await self._bus.emit(
            Events.GAME_UNINSTALLED,
            store="epic",
            game_id=game_id,
        )
        if not removed:
            return Result(
                success=False,
                error="uninstall_incomplete_files_remain",
            )
        return Result(success=True)

