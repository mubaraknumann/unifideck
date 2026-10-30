"""services/launcher/native_run.py — run a native Linux game and report how it ended.

Split out of ``helpers.py`` (volumetry) when the run gained its output log
and exit report. ``helpers`` still builds the argv, env and cwd; this
module spawns the process and says what happened.

A native game's stdout+stderr go to this launch's ``launches/<id>.game.log``,
the file umu runs already write and Capture Logs already collects. Before
this, a native game that died on start left nothing anywhere: Streets of
Rage 4's Linux build exited within a second on every launch, and neither
the exit code nor a line of its output was on disk.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.game_log import (
    game_log_path,
    open_game_log,
)

from .helpers import (
    _is_gog_dosbox_wrapper,
    build_native_argv,
    native_cwd,
    prepare_native_env,
)

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext, RuntimeState

    from .service import LauncherService

logger = logging.getLogger(__name__)

# A native game that fails this fast never reached a playable state. Same
# threshold the GOG Proton path uses to spot a launcher stub that bailed.
EARLY_EXIT_SECONDS = 15


async def run_native_subprocess(
    svc: LauncherService, ctx: LaunchContext, state: RuntimeState,
) -> int:
    """Set up and run a native Linux game's subprocess, returning its exit code.

    Chmods the executable bit (GOG's Linux depots don't always ship
    one set), builds the DOSBox-aware/Steam-Runtime-wrapped argv, and
    restores the Steam Overlay/Input env before spawning — see
    ``build_native_argv``/``prepare_native_env``.
    """
    try:
        await asyncio.to_thread(ctx.exe_path.chmod, 0o755)
    except OSError as e:
        logger.debug("[Helpers] chmod 755 failed on %s: %s", ctx.exe_path, e)

    env = prepare_native_env(ctx)
    cmd = build_native_argv(ctx, state, ctx.exe_path)
    logger.info("[Helpers] Spawning native launch: %s", cmd)

    if _is_gog_dosbox_wrapper(ctx):
        return await _run_dosbox_wrapper(svc, ctx, cmd, env)
    return await _run_game(svc, ctx, cmd, env)


async def _run_game(
    svc: LauncherService, ctx: LaunchContext, cmd: list[str], env: dict[str, str],
) -> int:
    """Run a native game with its output in the game log, then report the exit.

    The launcher has no terminal of its own (Steam pipes its output into
    console-linux.txt), so a file loses the game nothing a pipe gave it.
    """
    game_log = open_game_log()
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(native_cwd(ctx)),
            env=env,
            stdout=game_log,
            stderr=game_log,
        )
        svc._active_subprocess = proc
        try:
            rc = await proc.wait()
        finally:
            svc._active_subprocess = None
    finally:
        if game_log is not None:
            with contextlib.suppress(OSError):
                game_log.close()
    elapsed = time.monotonic() - started
    logger.info(
        "[Helpers] native game exited rc=%s after %.1fs (output → %s)",
        rc, elapsed, game_log_path() if game_log is not None else "inherited",
    )
    if rc != 0 and elapsed < EARLY_EXIT_SECONDS:
        await _emit_native_early_exit_toast(svc, ctx, rc)
    return rc


async def _run_dosbox_wrapper(
    svc: LauncherService, ctx: LaunchContext, cmd: list[str], env: dict[str, str],
) -> int:
    """Run the GOG DOSBox wrapper module, logging its stderr on failure.

    The wrapper is itself a Python subprocess that can fail before ever
    exec'ing the real game (bad PYTHONPATH, missing module). Its stderr is
    captured so that class of failure is logged instead of vanishing
    silently, as it did the first time this path actually ran (see
    ``prepare_native_env``'s PYTHONPATH fix).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(native_cwd(ctx)),
        env=env,
        stderr=asyncio.subprocess.PIPE,
    )
    svc._active_subprocess = proc
    try:
        _, stderr = await proc.communicate()
        if proc.returncode != 0 and stderr:
            logger.warning(
                "[Helpers] DOSBox wrapper exited %s: %s",
                proc.returncode, stderr.decode(errors="replace").strip(),
            )
        return proc.returncode or 0
    finally:
        svc._active_subprocess = None


async def _emit_native_early_exit_toast(
    svc: LauncherService, ctx: LaunchContext, rc: int,
) -> None:
    """Tell the user a native game closed right after it started.

    Without this, the only sign was the game vanishing back to the library.
    Best-effort: a toast failure never affects the launch result.
    """
    log_path = game_log_path()
    logger.warning(
        "[Helpers] native game %s:%s exited rc=%s within %ds; output in %s",
        ctx.store, ctx.game_id, rc, EARLY_EXIT_SECONDS, log_path,
    )
    try:
        from unifideck.launcher.rpc import emit_stage

        await emit_stage(
            svc._bus,
            i18n_key="toasts.launcher.nativeExitedEarly",
            i18n_title_key="toasts.header.gameLaunch",
            game_title=getattr(ctx, "game_key", None)
            or f"{ctx.store}:{ctx.game_id}",
            priority="normal",
            severity="warning",
            duration_ms=10000,
        )
    except Exception as e:  # never let a toast break a launch
        logger.debug("[Helpers] native early-exit toast failed: %s", e)
