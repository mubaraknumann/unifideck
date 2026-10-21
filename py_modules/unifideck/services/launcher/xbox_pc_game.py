"""Run an installed Xbox PC game, or say why a Microsoft title cannot run.

The launch itself is ``launcher/xbox_pc/session.py`` (stdlib only). This
module is its service-side shell: it resolves what that package may not
import (the Steam root and libraries, the Steam window identity, the UI
language, the game log), turns every refusal into a toast and maps the
result to the launcher's exit code.

Deliberately not the Proton orchestrator: no umu, no GE, no
``setup_prefix`` (I-017), and no ``cloud_sync_phase``, because the Xodus
runtime syncs Xbox saves itself. No circuit-breaker plumbing either: the
breaker is inert in the launcher process.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Result
from unifideck.launcher.xbox_pc.errors import (
    GAME_ARGS_IGNORED_KEY,
    STARTING_KEY,
    XboxPcLaunchError,
    toast_key,
)

from .error_toasts import emit_launch_error_toast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from unifideck.launcher.types.context import LaunchContext, RuntimeState
    from unifideck.launcher.xbox_pc.session import LaunchDeps

    from .service import LauncherService

logger = logging.getLogger(__name__)

_TAG = "XboxPc"


async def _refuse(svc: LauncherService, ctx: LaunchContext, err: XboxPcLaunchError) -> Result:
    """Toast the refusal and return its failure result."""
    logger.warning("[LauncherService] Xbox PC launch refused: %s", err)
    await emit_launch_error_toast(
        svc, ctx, i18n_key=toast_key(err.reason),
        extra_params={"reason": err.reason}, tag=_TAG,
    )
    code = f"xbox_pc_{err.reason}"
    return Result(success=False, error=code, error_code=code, store=ctx.store)


async def _notice(svc: LauncherService, ctx: LaunchContext, key: str, params: Mapping[str, Any]) -> None:
    """A non-error notice for this launch, titled with the game's name."""
    from unifideck.launcher.game_title import resolve_title
    from unifideck.launcher.rpc import emit_stage

    title = resolve_title(ctx.game_key)
    await emit_stage(
        svc._bus, i18n_key=key, game_title=title, priority="normal",
        i18n_params={"game_key": title, **params},
    )


def _window_env(ctx: LaunchContext) -> dict[str, str] | None:
    """The Steam window identity when the shortcut's app id is known, else None.

    None keeps the ``SteamGameId`` Steam gave the launcher. The all-zero
    block ``build_steam_window_env`` returns for an unknown id would
    overwrite it and leave the game behind the Gaming Mode loading screen.
    """
    from unifideck.core.compat_bridge import to_unsigned
    from unifideck.steam.window_env import build_steam_window_env

    try:
        unsigned = to_unsigned(ctx.steam_app_id) if ctx.steam_app_id else 0
    except (TypeError, ValueError):
        unsigned = 0
    if not unsigned:
        logger.info("[LauncherService] Xbox PC: no shortcut app id, keeping the inherited SteamGameId")
        return None
    return build_steam_window_env(unsigned, log_tag="launcher.xbox_pc")


def build_deps(svc: LauncherService, ctx: LaunchContext) -> LaunchDeps:
    """Everything the stdlib-only session needs from the rest of Unifideck."""
    from unifideck.launcher.proton.infrastructure.game_log import open_game_log
    from unifideck.launcher.proton.language_setup.resolver import get_unifideck_language
    from unifideck.launcher.xbox_pc.session import LaunchDeps
    from unifideck.services.launch_history.config_readers import read_fast_boot_seconds
    from unifideck.utils.vdf_compat import find_steam_root, steam_library_dirs

    async def notify(key: str, params: Mapping[str, Any]) -> None:
        await _notice(svc, ctx, key, params)

    return LaunchDeps(
        steam_root=find_steam_root(),
        steam_libraries=tuple(steam_library_dirs()),
        window_env=_window_env(ctx),
        language=get_unifideck_language(svc._config),
        log_file=open_game_log(),
        fast_fail_s=read_fast_boot_seconds(svc._config),
        notify=notify,
    )


async def _warn_dropped_args(svc: LauncherService, ctx: LaunchContext, state: RuntimeState) -> None:
    """``xodus-cli run`` has no way to pass game arguments yet. Say so."""
    if not state.game_args:
        return
    logger.warning(
        "[LauncherService] Xbox PC: %d game argument(s) dropped: %s",
        len(state.game_args), state.game_args,
    )
    await _notice(svc, ctx, GAME_ARGS_IGNORED_KEY, {})


async def run_xbox_pc_game(
    svc: LauncherService, ctx: LaunchContext, state: RuntimeState,
) -> Result:
    """Run the installed game through Xodus and wait for it to exit."""
    from unifideck.launcher.proton.infrastructure.container_escape import in_pressure_vessel

    if in_pressure_vessel():
        # A forced compatibility tool wraps the launcher in pressure-vessel,
        # where the memfd handoff and the host Steam root do not hold.
        return await _refuse(svc, ctx, XboxPcLaunchError("compat_tool_wrapped"))
    await _notice(svc, ctx, STARTING_KEY, {})
    await _warn_dropped_args(svc, ctx, state)
    try:
        rc = await _run_session(svc, ctx)
    except XboxPcLaunchError as err:
        return await _refuse(svc, ctx, err)
    logger.info("[LauncherService] Xbox PC game %s exited %d", ctx.game_key, rc)
    if rc == 0:
        return Result(success=True, store=ctx.store)
    return Result(success=False, error=f"exit_{rc}", error_code=f"exit_{rc}", store=ctx.store)


async def _run_session(svc: LauncherService, ctx: LaunchContext) -> int:
    """Run the session; ``GAME_LAUNCHED``/``GAME_STOPPED`` bracket the spawned game."""
    from unifideck.core.types.events import Events
    from unifideck.launcher.xbox_pc.session import XboxPcLaunch

    launched = False

    async def on_spawned() -> None:
        nonlocal launched
        launched = True
        await svc._bus.emit(
            Events.GAME_LAUNCHED, store=ctx.store, game_id=ctx.game_id,
            title="", app_id=0,  # LaunchContext carries neither
        )

    deps = build_deps(svc, ctx)
    try:
        return await XboxPcLaunch(ctx, _with_spawn_hook(deps, on_spawned)).run()
    finally:
        _close_log(deps)
        if launched:
            await svc._bus.emit(Events.GAME_STOPPED, store=ctx.store, game_id=ctx.game_id)


def _with_spawn_hook(deps: LaunchDeps, hook: Any) -> LaunchDeps:
    """``deps`` with ``on_spawned`` set."""
    from dataclasses import replace

    return replace(deps, on_spawned=hook)


def _close_log(deps: LaunchDeps) -> None:
    """Close the game log, if one was opened."""
    if deps.log_file is None:
        return
    try:
        deps.log_file.close()
    except OSError as err:
        logger.debug("[LauncherService] game log close failed: %s", err)


async def run_xbox_pc_notice(svc: LauncherService, ctx: LaunchContext) -> Result:
    """Launch nothing and say why: the toggle is off, or the game is not installed."""
    reason = ctx.notice_reason or "not_installed"
    return await _refuse(svc, ctx, XboxPcLaunchError(reason))
