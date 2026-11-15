"""EA launch: licence and LSX from a ``maxima-cli`` sidecar, the game under umu.

The game runs exactly like a ``generic.py`` raw-exe launch, so Steam tracks the
real game process (window focus in Gaming Mode, Stop, playtime, cloud sync).
Before it, two EA-only steps:

1. Touchup, when this prefix has not had it for the installed build
   (``launcher/ea/touchup.py``; normally done at the install warmup).
2. ``maxima-cli launch-prepare`` (``launcher/ea/sidecar.py``): requests the
   licence and serves LSX for the whole session; its environment is merged into
   the game's. It is stopped when the game exits, the launch fails, or Steam
   stops the game.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from unifideck.launcher.ea import maxima
from unifideck.launcher.ea.records import InstallRecord, read_record
from unifideck.launcher.ea.sidecar import Sidecar, SidecarError, start_sidecar
from unifideck.launcher.ea.standin import run_standin, wants_standin
from unifideck.launcher.ea.touchup import prepare_prefix
from unifideck.launcher.proton.compat.app_local_crt import ensure_crt_activation_manifest
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.game_exit import finish_game_run
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry
from unifideck.launcher.types.errors import (
    DependencyMissingError,
    GameFailedError,
    GameNotFoundError,
    PrefixCorruptedError,
)
from unifideck.utils.config_helpers import read_config_str_cold_start

logger = logging.getLogger(__name__)


async def ea_launch(plan: ProtonLaunchPlan) -> int:
    """Prepare the prefix, start the sidecar, run the game, stop the sidecar."""
    record, cli = await _resolve(plan)
    # Games that bundle a VC++ 2005/2008 runtime but declare no manifest die with R6034.
    await asyncio.to_thread(ensure_crt_activation_manifest, plan.context.exe_path)
    sidecar = await _start_sidecar(plan, record, cli)
    try:
        rc = await _run_game(plan, sidecar)
    finally:
        await sidecar.stop()
    return finish_game_run(plan, rc)


async def _resolve(plan: ProtonLaunchPlan) -> tuple[InstallRecord, Path]:
    """The install record and the ``maxima-cli`` to use, or the reason there are none."""
    ctx = plan.context
    record = await asyncio.to_thread(read_record, ctx.game_id)
    if record is None:
        raise GameNotFoundError("no EA install record", context={"store": "ea", "game_id": ctx.game_id})
    cli = maxima.find_cli(ctx.plugin_dir, read_config_str_cold_start(maxima.CLI_OVERRIDE_KEY))
    if cli is None:
        raise DependencyMissingError("maxima-cli not found", context={"store": "ea"})
    return record, cli


async def _start_sidecar(plan: ProtonLaunchPlan, record: InstallRecord, cli: Path) -> Sidecar:
    """Touchup when stale, then ``launch-prepare`` with the game's prefix."""
    ctx, state = plan.context, plan.state
    if not await prepare_prefix(ctx, state):
        raise PrefixCorruptedError("EA Touchup failed", context={"store": "ea", "game_id": ctx.game_id})
    assert state.prefix_path and state.proton_path and state.umu_wrapper  # prepare_prefix checked them
    env = {**plan.env, **maxima.base_env(), **maxima.prefix_env(state.prefix_path, state.proton_path, state.umu_wrapper)}
    try:
        return await start_sidecar(cli, record.offer_id, ctx.exe_path, env)
    except SidecarError as e:
        refusal = e.licence_refusal
        if refusal:
            # CG_LIMIT_EXCEEDED: too many activations of this game lately; EA lifts it with time.
            logger.warning("[EA] EA's licence service refused %s: %s", record.offer_id, refusal)
        raise GameFailedError(
            f"EA refused the game licence ({refusal})" if refusal else "EA licence or LSX unavailable",
            subprocess_rc=e.returncode or -1,
            context={"store": "ea", "game_id": ctx.game_id, "detail": e.detail[-300:]},
        ) from e


async def _run_game(plan: ProtonLaunchPlan, sidecar: Sidecar) -> int:
    """The game under umu, with the Origin client stand-in beside it when there is one."""
    exe = plan.context.exe_path
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(exe), *plan.state.game_args]
    cwd = exe.parent if exe.parent.is_dir() else None
    env = {**plan.env, **sidecar.env}
    game_done = asyncio.Event()
    standin = None
    wanted = bool(sidecar.origin_standin and plan.state.prefix_path)
    if wanted:
        wanted = await asyncio.to_thread(wants_standin, plan.context.game_id, exe)
    if wanted and sidecar.origin_standin and plan.state.prefix_path:
        standin = asyncio.create_task(run_standin(
            [str(plan.python_bin), str(plan.umu_wrapper), sidecar.origin_standin],
            env, exe.name, plan.state.prefix_path, game_done,
        ))
    try:
        return await run_umu_with_retry(argv, env=env, cwd=cwd, on_start=plan.on_process_start)
    finally:
        game_done.set()
        if standin is not None:
            standin.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await standin
