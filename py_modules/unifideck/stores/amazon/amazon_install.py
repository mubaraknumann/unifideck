"""Amazon Games installer — install / uninstall pipeline using nile.

``AmazonInstaller`` orchestrates installs and uninstalls via the
``nile`` CLI. The install pipeline :

1. **preflight** — verify nile binary, resolve base path, build the
   install context;
2. **probe** — query nile for the game's manifest (size, fuel.json
   location, supported architectures);
3. **subprocess** — spawn nile with structured progress callbacks
   (parses nile's stdout for "downloaded X/Y bytes" lines);
4. **finalize** — parse the fuel.json from ``amazon_fuel.py``
 to extract the launch executable, write the
   ``.unifideck-id`` marker, register with the install registry.

The uninstall path is symmetric : remove install dir, drop registry
entry, clean up shortcut + artwork cache.

Errors are wrapped into typed ``InstallResult`` envelopes ; partial
installs are cleaned up to avoid leaving orphaned files on disk.
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
from typing import Any, cast

from unifideck.core.binaries import clean_cli_env
from unifideck.core.manifest import write_manifest
from unifideck.core.safe_delete import canonical_prefix, safe_rmtree
from unifideck.core.types import Events, InstallResult, Result
from unifideck.event_bus.event_bus import EventBus
from unifideck.stores.shared.cli_install_helpers import (
    DEFAULT_STALL_TIMEOUT_S,
    InstallStalledError,
    TailRingBuffer,
    drain_install_output,
    join_tail,
    parse_percent_re,
    parse_transfer_progress,
    terminate_process_tree,
    wait_with_timeout,
)

from . import amazon_fuel
from .amazon_library import AmazonLibraryReader

logger = logging.getLogger(__name__)
# Nile's ProgressBar emits lines like:
#   = Progress: 42.50 123456789/987654321, Running for: 00:01:30, ETA: ...
# which is byte-for-byte the shape gogdl emits, so ``parse_transfer_progress``
# handles both (audit §3.2 — this used to be two near-verbatim copies).
# The bracket regex is a fallback for any tool that emits `[ NN% ]`.
_PROGRESS_RE_BRACKET = re.compile(r"\[\s*([\d.]+)\s*%\s*\]")
ProgressCallback = Callable[[Any], Awaitable[None]]

@dataclass
class _RunOutcome:
    """Result of one ``nile install`` subprocess run.

    ``rc`` is the exit code (``-1`` on timeout, ``-2`` when the process
    never started); ``tail`` is the last few non-progress output lines
    captured by the ring buffer — nile's real error text when ``rc != 0``,
    so the failure isn't reduced to a bare ``nile_exit_{rc}``.
    ``spawn_error`` is set only when ``create_subprocess_exec`` itself
    raised, in which case there is no exit code to report at all.
    """

    rc: int
    tail: str = ""
    spawn_error: str | None = None

def _format_exit_error(outcome: _RunOutcome) -> str:
    """Turn a failed run into an error string.

    Keeps the machine-parsable ``nile_exit_{rc}`` prefix (matched by
    downstream classification / callers) and appends nile's actual output
    tail when captured, so logs, the tracker, and the download item's
    ``error_message`` name the real reason instead of a bare code.

    Bug report: four Amazon installs failed after ~6s with 0 bytes and the
    UI showed only "Failed" — every install line, including whatever nile
    printed as its actual error, went to a DEBUG log the reporter never
    captured. Mirrors the identical fix made for Epic/legendary.
    """
    if outcome.spawn_error:
        return f"nile_spawn_failed: {outcome.spawn_error}"
    base = f"nile_exit_{outcome.rc}"
    return f"{base}: {outcome.tail}" if outcome.tail else base

class AmazonInstaller:
    """Amazon installer."""

    def __init__(
        self,
        bus: EventBus,
        cli_path: str | None,
        library: AmazonLibraryReader,
        find_exe: Callable[[str, list[str] | None], str | None],
        default_install_root: str,
        install_timeout_seconds: int = 3600,
        uninstall_timeout_seconds: int = 120,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._cli_path = cli_path
        self._library = library
        self._find_exe = find_exe
        self._default_install_root = str(Path(default_install_root).expanduser())
        self._install_timeout = install_timeout_seconds
        self._uninstall_timeout = uninstall_timeout_seconds

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
        verb: str = "install",
    ) -> InstallResult:
        """Install or update a game.

        ``verb="update"`` runs ``nile update`` (the genuine update
        command — an alias of ``install`` in nile) for an in-place
        patch; the rest of the pipeline (path resolution, manifest
        rewrite, events) is identical to a fresh install.
        """
        logger.info("[AmazonInstall] %s game_id=%s base_path=%s", verb, game_id, base_path)
        if not self._cli_path:
            return InstallResult(
                success=False,
                error="nile_not_found",
                store="amazon",
                game_id=game_id,
            )
        base = base_path or self._default_install_root
        try:
            await asyncio.to_thread(lambda: Path(base).mkdir(parents=True, exist_ok=True))
        except OSError as e:
            return InstallResult(
                success=False,
                error=f"mkdir_failed: {e}",
                store="amazon",
                game_id=game_id,
            )
        outcome = await self._run_install(base, game_id, progress_cb, verb)
        if outcome.rc != 0:
            error = _format_exit_error(outcome)
            logger.error(
                "[AmazonInstall] %s failed for %s: %s",
                verb, game_id, outcome.tail or "(no output captured)",
            )
            return InstallResult(
                success=False,
                error=error,
                store="amazon",
                game_id=game_id,
            )
        return await self._finalize_install(game_id, base, outcome.tail)

    async def _finalize_install(
        self, game_id: str, base: str, tail: str = "",
    ) -> InstallResult:
        """Finalize install — locate the installed directory and write manifest.

        Nile may record the install path in its installed.json before
        the directory is fully materialized, or use a folder name that
        differs from both the game ID and title. ``_resolve_install_path``
        verifies the directory exists on disk before returning it.
        If we still can't locate the install directory after the CLI
        reported success, the install is incomplete and we report failure.

        ``tail`` is nile's own output for the run. It matters most on this
        exact-zero-exit failure: when a stale cached manifest makes nile
        no-op it prints "Game is up to date" and exits 0, and discarding
        that line left the bare code ``install_dir_not_found`` as the only
        evidence — the reason had to be recovered from nile's source
        instead of from our own logs.
        """
        install_path = await self._resolve_install_path(game_id, base)
        if not install_path:
            error = f"install_dir_not_found: {tail}" if tail else "install_dir_not_found"
            logger.error(
                "[AmazonInstall] cannot locate install directory for %s "
                "under %s — nile reported success but no matching "
                "directory found on disk; nile said: %s",
                game_id, base, tail or "(no output captured)",
            )
            return InstallResult(
                success=False,
                error=error,
                store="amazon",
                game_id=game_id,
            )
        exe = await self._resolve_executable(install_path, game_id)
        title = await self._resolve_title(game_id)
        exe_relative = ""
        if exe:
            with contextlib.suppress(ValueError):
                exe_relative = os.path.relpath(exe, install_path)  # noqa: ASYNC240
        try:
            await write_manifest(
                install_dir=install_path,
                store="amazon",
                store_id=game_id,
                title=title,
                executable_relative=exe_relative,
                platform="windows",
            )
        except OSError as exc:
            logger.exception("[AmazonInstall] write_manifest failed for %s", install_path)
            return InstallResult(
                success=False,
                error=f"manifest_write: {exc}",
                store="amazon",
                game_id=game_id,
            )
        # No ``DOWNLOAD_COMPLETE`` here — see the same note in
        # ``stores/epic/install.py``. ``DownloadWorker`` owns every
        # ``DOWNLOAD_*`` emit for all seven stores; the install is still
        # mid-flight at this point because prefix warmup has not run yet,
        # and only the worker's payload carries the ``Game`` record the
        # shortcut service needs to flip the install tag.
        return InstallResult(
            success=True,
            store="amazon",
            game_id=game_id,
            install_path=install_path,
        )

    async def _run_install(
        self,
        base: str,
        game_id: str,
        progress_cb: ProgressCallback | None,
        verb: str = "install",
    ) -> _RunOutcome:
        """Run install or update (``verb``), capturing nile's output tail."""
        self._current_progress = {
            "progress_percent": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed_bps": 0.0,
            "eta_seconds": 0,
        }
        cmd = self._build_install_cmd(base, game_id, verb)
        logger.info("[AmazonInstall] executing: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=clean_cli_env(),
            )
        except OSError as e:
            # A lost exec bit / missing loader on bin/nile would otherwise
            # escape to the worker and surface as a generic "unknown_error",
            # which is visually identical to a genuine instant failure.
            # The uninstall path has always reported this properly.
            logger.exception("[AmazonInstall] cannot spawn %s", cmd[0])
            return _RunOutcome(rc=-2, spawn_error=str(e))
        tail_buf = TailRingBuffer()
        stalled: InstallStalledError | None = None
        drain_exc: BaseException | None = None
        try:
            await self._drain_install_output(
                proc,
                game_id,
                progress_cb,
                tail_buf,
            )
        except InstallStalledError as e:
            stalled = e
        except BaseException as e:
            drain_exc = e
        if stalled is not None or drain_exc is not None:
            # Kill the tree BEFORE waiting, which is the whole fix here.
            # Cancelling this task only unwinds our coroutine; nile keeps
            # running and keeps writing into the game directory after the
            # row already reads "Cancelled". Worse, the wait below used to
            # come first, so a cancel blocked for the full install timeout
            # (an hour) — and ``DownloadService.cancel`` awaits this task,
            # so the cancel RPC itself hung with it. Epic has always had
            # these two steps in this order.
            await terminate_process_tree(proc, "[amazon_install]")
        if drain_exc is not None:
            raise drain_exc
        if stalled is not None:
            return _RunOutcome(rc=-1, tail=join_tail(str(stalled), tail_buf))
        rc = await self._wait_with_timeout(proc)
        return _RunOutcome(rc=rc, tail=tail_buf.tail())

    def _build_install_cmd(self, base: str, game_id: str, verb: str = "install") -> list[str]:
        """Build install/update cmd.

        ``verb`` is ``"install"`` for a fresh install or ``"update"``
        for an in-place update. In nile, ``update`` is an alias of
        ``install`` (identical args/output) — running it on an
        already-installed game patches it in place.
        """
        if self._cli_path is None:
            raise RuntimeError(
                "nile CLI path is not set; cannot build install cmd",
            )
        return [
            self._cli_path,
            verb,
            game_id,
            "--base-path",
            base,
        ]

    async def _drain_install_output(
        self,
        proc: Any,
        game_id: str,
        progress_cb: ProgressCallback | None,
        tail_buf: TailRingBuffer,
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
            "[amazon_install]",
        )

    async def _handle_install_line(
        self,
        line: str,
        game_id: str,
        progress_cb: ProgressCallback | None,
        *,
        tail_buf: TailRingBuffer,
    ) -> None:
        """Handle install line.

        Non-progress lines (where nile prints its actual error) are fed to
        ``tail_buf`` so a failing install can surface the real reason
        instead of a bare exit code.
        """
        updated = parse_transfer_progress(line, self._current_progress)
        if not updated:
            # Fallback to check bracket format
            pct = parse_percent_re(line, _PROGRESS_RE_BRACKET)
            if pct is not None:
                self._current_progress["progress_percent"] = pct
                updated = True

        if not updated:
            tail_buf.append(line)
            logger.debug("[nile install] %s", line)
            return

        # The callback is the only report: it merges this tick onto the
        # queue item (``worker_helpers.apply_dict_progress`` reads both
        # ``progress_percent`` and ``speed_bps``) and the worker emits the
        # one ``DOWNLOAD_PROGRESS`` from there.
        if progress_cb is not None:
            try:
                await progress_cb(dict(self._current_progress))
            except Exception as e:
                logger.debug(
                    "[amazon_install] progress_cb raised: %s",
                    e,
                )

    async def _resolve_install_path(self, game_id: str, base: str) -> str | None:
        """Resolve install path from nile's installed.json or fallback.

        Nile writes an entry to installed.json before (or during)
        the download, and the recorded path may not exist on disk
        yet if nile failed mid-flight or recorded an alternate
        directory name. Always verify the directory exists before
        returning a path — a stale entry that points nowhere
        must fall through to the default-path check.
        """
        installed = await self._library.read_installed_ids()
        info = installed.get(game_id)
        if info and info.get("path"):
            candidate = cast("str | None", info["path"])
            if candidate:
                if await asyncio.to_thread(Path(candidate).is_dir):
                    return candidate
        default = str(Path(base) / game_id)
        if await asyncio.to_thread(Path(default).is_dir):
            return default
        # Nile may create a subdirectory named after the game title
        # rather than the game_id. Scan the base directory for any
        # subdirectory that contains a .unifideck-id marker or
        # matches a known pattern from nile's fuel.json.
        title = await self._resolve_title(game_id)
        if title and title != game_id:
            title_path = str(Path(base) / title)
            if await asyncio.to_thread(lambda: Path(title_path).is_dir()):
                return title_path
        return None

    async def _resolve_executable(
        self,
        install_path: str | None,
        game_id: str,
    ) -> str | None:
        """Resolve executable."""
        if not install_path:
            return None
        from_fuel = amazon_fuel.find_exe_from_fuel(install_path)
        if from_fuel:
            return from_fuel
        return self._find_exe(install_path, [game_id])

    async def _resolve_title(self, game_id: str) -> str:
        """Resolve title."""
        owned = await self._library.read_owned_games()
        for game in owned:
            if game.store_game_id == game_id:
                return game.title
        return game_id

    async def uninstall_game(
        self, game_id: str, delete_prefix: bool = False,
    ) -> Result:
        """Uninstall game.

        ``nile uninstall`` only removes the files *it* tracks: it leaves our
        own ``.unifideck_manifest.json`` marker behind (so the install dir
        survives as a stub) and never touches the Proton prefix. On failure
        it deletes nothing at all. So — like Epic — we resolve the install
        dir up front (nile drops its ``installed.json`` entry on success),
        run nile best-effort, then delete any directory + prefix it leaves
        behind ourselves so nothing is orphaned.
        """
        # Resolve the install dir while nile's installed.json entry is intact.
        install_path = await self._resolve_install_path(
            game_id, self._default_install_root,
        )

        nile_error = (
            await self._run_nile_uninstall(game_id)
            if self._cli_path
            else "nile_not_found"
        )

        # Fallback cleanup: remove the leftover stub dir (our manifest marker)
        # and, if requested, the per-game Proton prefix.
        removed = await self._ensure_install_dir_gone(install_path)
        if delete_prefix:
            await asyncio.to_thread(safe_rmtree, canonical_prefix(game_id))

        if not removed:
            return Result(
                success=False,
                error=nile_error or "uninstall_incomplete_files_remain",
            )
        await self._bus.emit(
            Events.GAME_UNINSTALLED,
            store="amazon",
            game_id=game_id,
        )
        return Result(success=True)

    async def _run_nile_uninstall(self, game_id: str) -> str | None:
        """Run ``nile uninstall`` best-effort. Returns an error str or None."""
        try:
            proc = await asyncio.create_subprocess_exec(
                cast("str", self._cli_path),
                "uninstall",
                game_id,
                "--yes",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
        except OSError as e:
            logger.warning("[AmazonUninstall] could not spawn nile: %s", e)
            return f"nile_spawn_failed: {e}"
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._uninstall_timeout,
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            logger.warning("[AmazonUninstall] nile uninstall timed out")
            return "uninstall_timeout"
        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:200]
            logger.warning("[AmazonUninstall] nile uninstall failed: %s", err)
            return f"uninstall_failed: {err}"
        return None

    async def _ensure_install_dir_gone(
        self, install_path: str | None,
    ) -> bool:
        """Delete the install dir if it survived nile. True if gone after."""
        if not install_path:
            # Nothing tracked to delete — treat as already removed.
            return True
        p = Path(install_path)
        if not await asyncio.to_thread(p.exists):
            return True
        logger.info("[AmazonUninstall] removing leftover install dir: %s", p)
        return await asyncio.to_thread(safe_rmtree, p)
