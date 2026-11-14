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
import logging
from pathlib import Path

from unifideck.launcher.ea import maxima
from unifideck.launcher.ea.records import InstallRecord, read_record
from unifideck.launcher.ea.sidecar import Sidecar, SidecarError, start_sidecar
from unifideck.launcher.ea.touchup import prepare_prefix
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
    sidecar = await _start_sidecar(plan, record, cli)
    try:
        rc = await _run_game(plan, sidecar.env)
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
        raise GameFailedError(
            "EA licence or LSX unavailable", subprocess_rc=e.returncode or -1,
            context={"store": "ea", "game_id": ctx.game_id, "detail": e.detail[-300:]},
        ) from e


async def _run_game(plan: ProtonLaunchPlan, launch_env: dict[str, str]) -> int:
    exe = plan.context.exe_path
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(exe), *plan.state.game_args]
    cwd = exe.parent if exe.parent.is_dir() else None
    return await run_umu_with_retry(
        argv, env={**plan.env, **launch_env}, cwd=cwd, on_start=plan.on_process_start,
    )
