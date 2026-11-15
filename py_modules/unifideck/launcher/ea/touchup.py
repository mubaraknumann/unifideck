"""Touchup: the build's own installer script, run in the game's prefix.

An EA build ships ``__Installer/touchup.exe``. It writes the registry keys the
game reads (``Install Dir`` and friends) and installs its redistributables, so a
game is not playable until it has run in that game's prefix. ``maxima-cli
touchup`` runs it under the Proton and umu this launch resolved.

It runs once per installed build and prefix: the marker in the prefix records
which build it ran for. Called after ``setup_prefix`` at install time (the
download warmup, through ``launcher/proton/store_setup.py``) and again before a
launch only when the marker is stale.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.core.binaries import clean_cli_env
from unifideck.utils.config_helpers import read_config_str_cold_start

from . import maxima
from .records import InstallRecord, read_record
from .touchup_restore import match_ledger_case, restore_moved_files

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext, RuntimeState

logger = logging.getLogger(__name__)

MARKER = ".unifideck_ea_touchup"
TOUCHUP_TIMEOUT_S = 1800.0
_TAIL_CHARS = 800


def _stamp(record: InstallRecord) -> dict[str, str]:
    return {"build_id": record.build_id, "install_path": record.install_path}


def touchup_needed(prefix: Path, record: InstallRecord) -> bool:
    """Whether Touchup has not yet run in ``prefix`` for this build and folder."""
    try:
        return bool(json.loads((prefix / MARKER).read_text()) != _stamp(record))
    except (OSError, ValueError):
        return True


async def run_touchup(
    record: InstallRecord, prefix: Path, proton_script: Path, umu_run: Path,
    plugin_dir: str | Path | None, session_env: dict[str, str] | None = None,
) -> bool:
    """Run Touchup for ``record`` in ``prefix``; True when it succeeded.

    ``session_env`` fills in the display and D-Bus the headless plugin lacks
    (without a display Wine's desktop never starts and the run hangs); it never
    replaces a value already set.
    """
    cli = maxima.find_cli(plugin_dir, read_config_str_cold_start(maxima.CLI_OVERRIDE_KEY))
    if cli is None:
        return False
    env = clean_cli_env()
    for key, value in (session_env or {}).items():
        env.setdefault(key, value)
    env.update({**maxima.base_env(), **maxima.prefix_env(prefix, proton_script, umu_run)})
    # A new prefix for a build Touchup already ran on: its moves must be undone first.
    await asyncio.to_thread(restore_moved_files, Path(record.install_path))
    rc, output = await _run_cli(cli, ["touchup", "--path", record.install_path], env)
    if rc != 0:
        logger.error("[EA] Touchup failed for %s (exit %s): %s", record.game_id, rc, output[-_TAIL_CHARS:].strip())
        return False
    await asyncio.to_thread(match_ledger_case, Path(record.install_path))
    await asyncio.to_thread((prefix / MARKER).write_text, json.dumps(_stamp(record)))
    logger.info("[EA] Touchup done for %s (build %s)", record.game_id, record.version)
    return True


async def _run_cli(cli: Path, args: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run ``maxima-cli`` to its end in the empty run folder: ``(exit code, output)``."""
    # Imported here: ``launcher.proton`` imports the EA handler, which imports this module.
    from unifideck.launcher.proton.infrastructure.container_escape import escape_argv

    run_dir = await asyncio.to_thread(maxima.prepare_run_dir)
    # maxima-cli starts umu, which must not run nested in Steam's container.
    argv = escape_argv([str(cli), *args], env, cwd=run_dir)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env, cwd=str(run_dir), start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), TOUCHUP_TIMEOUT_S)
    except BaseException:
        # Its own session: the group holds umu and Wine too, not just the CLI.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, output.decode(errors="replace")


async def prepare_prefix(
    ctx: LaunchContext, state: RuntimeState, session_env: dict[str, str] | None = None,
) -> bool:
    """Run Touchup for this game when its prefix has not had it for this build.

    True when the prefix is ready (Touchup ran now or earlier). Needs the
    prefix, Proton and umu that ``setup_prefix`` resolved into ``state``.
    """
    record = await asyncio.to_thread(read_record, ctx.game_id)
    if record is None:
        logger.error("[EA] no install record for %s, so Touchup cannot run", ctx.game_id)
        return False
    if state.prefix_path is None or state.proton_path is None or state.umu_wrapper is None:
        logger.error("[EA] Touchup for %s skipped: the prefix, Proton or umu was not resolved", ctx.game_id)
        return False
    if not await asyncio.to_thread(touchup_needed, state.prefix_path, record):
        return True
    return await run_touchup(
        record, state.prefix_path, state.proton_path, state.umu_wrapper, ctx.plugin_dir, session_env,
    )
