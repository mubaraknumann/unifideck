"""gogdl's conditional repair / verify pass.

Split out of ``progress.py`` at the 550-LOC file cap. The two are genuinely
separate concerns: ``progress.py`` watches a *download*, this watches a
``gogdl repair`` — a full read-back that re-hashes every file against the
manifest and re-downloads mismatches.

It is deliberately **conditional**: an unconditional repair after every
install froze large games at 100%
(``gog-install-100pct-hang-conditional-repair``), so it now runs only when
the cheap completeness check fails. A non-zero repair code is equally
deliberately non-fatal — treating a failed *verification* as a failed
install is what once deleted an 87 GB game
(``gog-reinstall-repair-crash-and-prefix-warmup-hang``).

Mixed into ``_GogdlProgressMonitor`` rather than held as a separate object,
so ``installer.py`` keeps calling ``run_gogdl_repair_pass`` on the monitor.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.stores.shared.cli_install_helpers import (
    InstallStalledError,
    drain_install_output,
    parse_transfer_progress,
    terminate_process_tree,
)

from .primitives import GOGFolderOps

if TYPE_CHECKING:
    from .installer import GOGInstaller

logger = logging.getLogger(__name__)

# Tolerated silence for the conditional repair pass. gogdl `repair` re-hashes
# every file as ONE silent block (~11 min for ~53 GB on microSD in the field
# logs, scales with game size / disk speed). Repair must be allowed to finish,
# so this is deliberately generous — it only guards a truly wedged process.
_GOGDL_REPAIR_TIMEOUT_S = 3600.0


class _GogdlRepairMixin:
    """The ``gogdl repair`` half of the progress monitor."""

    _parent: GOGInstaller

    async def run_gogdl_repair_pass(
        self,
        game_id: str,
        platform: str,
        base_path: str,
        folder_name: str | None,
        preferred_lang: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Run GOGDL repair pass.

        ``repair`` re-reads every file and re-hashes it against the manifest,
        re-downloading mismatches — a full read-back over the whole game. It is
        run *conditionally* (only when a download came up short), so when it
        does run we surface it as an indeterminate "Verifying…" phase with live
        percent text rather than leaving the row frozen at 100%.
        """
        repair_path = self._resolve_repair_path(
            game_id,
            base_path,
            folder_name,
        )
        try:
            env, creds_path, _gogdl_cleanup = await self._parent._tokens.acquire_gogdl_creds()
            cmd = [
                self._parent._gogdl_bin,
                "--auth-config-path",
                creds_path,
                "repair",
                game_id,
                "--platform",
                platform,
                "--path",
                repair_path,
                "--lang",
                preferred_lang,
                "--with-dlcs",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
            except OSError as e:
                logger.warning(
                    "[GOGInstaller] could not spawn repair: %s",
                    e,
                )
                await _gogdl_cleanup()
                return
            try:
                await self._watch_repair(proc, progress_cb)
            finally:
                await _gogdl_cleanup()
        except Exception as e:
            logger.warning(
                "[GOGInstaller] repair pipeline failed: %s",
                e,
            )

    async def _watch_repair(
        self,
        proc: asyncio.subprocess.Process,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Drain a running ``gogdl repair``, killing its tree if it wedges.

        Split out of :meth:`run_gogdl_repair_pass` at the 80-line function
        cap. A non-zero repair code is deliberately non-fatal: the caller
        re-verifies afterwards, and treating a failed *verification* as a
        failed install is what once deleted an 87 GB game
        (``gog-reinstall-repair-crash-and-prefix-warmup-hang``).
        """
        try:
            await self._read_repair_loop(proc, progress_cb)
        except InstallStalledError as e:
            logger.warning("[GOGInstaller] repair %s — killing", e)
            await terminate_process_tree(proc, "[GOGInstaller repair]")
        except BaseException:
            await terminate_process_tree(proc, "[GOGInstaller repair]")
            raise
        else:
            try:
                await asyncio.wait_for(
                    proc.wait(), timeout=_GOGDL_REPAIR_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    "[GOGInstaller] repair did not exit (%ds) — killing",
                    int(_GOGDL_REPAIR_TIMEOUT_S),
                )
                await terminate_process_tree(proc, "[GOGInstaller repair]")
        if proc.returncode not in (0, None):
            logger.warning(
                "[GOGInstaller] repair code %d (non-fatal)",
                proc.returncode,
            )

    async def _read_repair_loop(
        self,
        proc: asyncio.subprocess.Process,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Drain repair stdout, reporting a "Verifying…" phase.

        Reuses the shared ``parse_transfer_progress`` (repair emits the same
        ``Progress:`` format as download). Guarded by the repair-phase stall
        window so a wedged repair can't read forever — ``gogdl repair``
        re-hashes every file as one silent block, so that window is
        deliberately generous. Raises :class:`InstallStalledError` on a stall; the
        caller kills the tree and bounds ``proc.wait()``.
        """
        progress: dict[str, Any] = {
            "progress_percent": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed_bps": 0.0,
            "eta_seconds": 0,
            "phase": "verifying",
        }
        await drain_install_output(
            proc,
            "",
            progress_cb,
            functools.partial(self._handle_repair_line, progress=progress),
            stall_s=_GOGDL_REPAIR_TIMEOUT_S,
        )

    @staticmethod
    async def _handle_repair_line(
        line_str: str,
        _game_id: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
        *,
        progress: dict[str, Any],
    ) -> None:
        """Route one ``gogdl repair`` output line."""
        if not line_str.startswith("[gogdl]"):
            logger.info("[gogdl-verify] %s", line_str)
        if progress_cb is None or "Progress:" not in line_str:
            return
        parse_transfer_progress(line_str, progress)
        progress["phase"] = "verifying"
        try:
            await progress_cb(dict(progress))
        except Exception as e:
            logger.debug("[GOGInstaller] verify phase_cb: %s", e)

    @staticmethod
    def _resolve_repair_path(
        game_id: str,
        base_path: str,
        folder_name: str | None,
    ) -> str:
        """Resolve repair path."""
        if folder_name:
            predicted = str(Path(base_path) / folder_name)
            if Path(predicted).exists():
                return predicted
        with contextlib.suppress(OSError):
            for name in [entry.name for entry in Path(base_path).iterdir()]:
                candidate = str(Path(base_path) / name)
                if not Path(candidate).is_dir():
                    continue
                if GOGFolderOps.has_goggame_info(
                    candidate,
                    game_id,
                ):
                    return candidate
        logger.warning(
            "[GOGInstaller] could not resolve repair path, using base_path",
        )
        return base_path
