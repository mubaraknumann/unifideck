"""services/launcher/orchestrator.py — Per-platform launch entry points.

2 public orchestrators:
- ``launch_windows`` — Proton-wrapped pipeline (prepare plan,
  sync down, run subprocess, sync up).
- ``launch_native`` — native Linux, simpler: cloud sync wraps
  a direct subprocess, no Proton/umu/prefix setup.
``LauncherService.launch`` dispatches between them based on
``ctx.is_windows_game``. Heavy lifting in ``helpers.py``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Result

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
    from unifideck.launcher.types.context import LaunchContext, RuntimeState

    from .service import LauncherService

logger = logging.getLogger(__name__)


async def launch_windows(
    svc: LauncherService,
    ctx: LaunchContext,
    state: RuntimeState,
) -> Result:
    """Windows game launch — 4-phase pipeline.

    1. ``prepare_windows_plan`` — runtime + umu + proton_prepare
    2. ``cloud_sync_phase("down")``
    3. ``run_game_subprocess`` — the actual game
    4. ``cloud_sync_phase("up")``
    """
    try:
        # Phase 1: Prepare
        plan = await svc._prepare_windows_plan(ctx, state)

        from unifideck.core.types.events import Events
        store = ctx.store
        game_id = ctx.game_id

        # Phase 1.5: Run the canonical prefix setup BEFORE the cloud sync-down.
        # ``setup_prefix`` is the SAME self-healing process install-time warmup
        # runs — createprefix + generic compat (winetricks + VC++ registry fix)
        # with the managed-GE recovery ladder, which on a genuine hang pins the
        # Proton it recovered to (the old split ran only createprefix here +
        # compat later in ``dispatch`` with no recovery, so launch re-picked the
        # hanging global-default Proton, saw a "family change" against the
        # GE-warmed prefix, and wiped + rebuilt it at Play time). Must precede
        # sync-down: the save dir resolves out of ``drive_c`` (e.g. GOG's
        # ``<?DOCUMENTS?>\\<title>``), which only exists after ``createprefix``.
        # Idempotent — a no-op once the prefix is set up. ``session_env`` is
        # None: at launch Steam already provides the user session. The returned
        # plan is authoritative for the launch (see the helper's docstring).
        plan = await _setup_prefix_and_realign(svc, ctx, state)

        # Phase 2: Cloud Sync Down
        await svc._cloud_sync_phase(ctx, "down")

        # Pre-launch event
        await svc._bus.emit(
            Events.GAME_LAUNCHED,
            store=store,
            game_id=game_id,
            title="",  # No title on LaunchContext
            app_id=0  # No app_id on LaunchContext
        )

        # Phases 3-4: Run Subprocess, then Cloud Sync Up
        return await _run_and_finalize(svc, ctx, state, plan, store, game_id)

    except Exception:
        logger.exception("[Orchestrator] Windows launch failed")
        raise  # Let the outer _handle_launcher_error catch and toast it


async def _setup_prefix_and_realign(
    svc: LauncherService,
    ctx: LaunchContext,
    state: RuntimeState,
) -> ProtonLaunchPlan:
    """Run the canonical prefix setup; return the plan the game must launch with.

    ``setup_prefix`` both mutates ``state`` (it builds its own plans, possibly
    under a borrowed Proton) and can conclude via its hang-recovery ladder that
    a DIFFERENT Proton is the working one. Either way the Phase-1 plan is no
    longer trustworthy, so rebuild it from the tool setup actually settled on.

    Launching the stale plan is the bug this exists to prevent: the launcher
    logged ``proton=GE-Proton11-3`` while umu ran the user's
    Proton-Experimental, so every launch re-stamped the prefix
    ("Upgrading prefix from X to Y" / "Prefix has an invalid version?!") and
    erased the VC++ registry keys compat had just imported.
    """
    from unifideck.launcher.proton import setup_prefix

    initial_tool = state.proton_tool_id
    final_tool, _recovered = await setup_prefix(ctx, state)
    tool = final_tool or initial_tool
    if tool != initial_tool:
        logger.info(
            "[Orchestrator] prefix setup settled on proton=%s (launch resolved "
            "%s) — rebuilding the plan so the game runs under it",
            tool, initial_tool,
        )
    # Rebuild unconditionally: even when the tool is unchanged, ``state`` may
    # still carry the borrowed Proton's path/wrapper, and plan and state must
    # agree for cancellation and the dispatch log to mean anything.
    return await svc._prepare_windows_plan(ctx, state, tool_id=tool)


async def _run_and_finalize(
    svc: LauncherService,
    ctx: LaunchContext,
    state: RuntimeState,
    plan: ProtonLaunchPlan,
    store: str,
    game_id: str,
) -> Result:
    """Phases 3-4 of :func:`launch_windows` — run the game, then sync up.

    Split out purely to keep ``launch_windows`` under the function-length
    cap; the ordering (GAME_STOPPED in a ``finally``, sync-up after it,
    exit code encoded last) is load-bearing and unchanged.
    """
    from unifideck.core.types.events import Events

    # Phase 3: Run Subprocess
    try:
        rc = await svc._run_game_subprocess(plan, ctx, state)
        state.rc = rc
    finally:
        # NOTE: this fires on the launcher SUBPROCESS bus (dispatcher.py),
        # which only forwards LAUNCHER_STAGE — it does NOT reach the
        # plugin's PlaytimeService. Playtime is recorded on the plugin bus
        # via the frontend lifetime listener → notify_game_stopped RPC.
        # Kept only for any future in-subprocess subscriber.
        await svc._bus.emit(Events.GAME_STOPPED, store=store, game_id=game_id)

    # Phase 4: Cloud Sync Up
    await svc._cloud_sync_phase(ctx, "up")

    exit_code = svc._resolve_exit_code(state)
    # ``Result`` has no ``rc`` field — its public surface is
    # ``success``, ``error``, ``error_code``, ``store``,
    # ``metadata``. The dispatcher's ``_map_result_to_exitcode``
    # parses ``error_code`` and extracts the integer from any
    # ``exit_<N>`` prefix; encoding the exit code there is the
    # documented round-trip channel for subprocess return codes.
    # The earlier ``rc=exit_code`` form raised
    # ``TypeError: Result.__init__() got an unexpected keyword
    # argument 'rc'`` on every launch — Windows games could
    # never report their exit code back to the launcher.
    return Result(
        success=(exit_code == 0),
        error_code=None if exit_code == 0 else f"exit_{exit_code}",
    )


async def launch_native(
    svc: LauncherService,
    ctx: LaunchContext,
    state: RuntimeState,
) -> Result:
    """Native Linux game launch — simpler path.

    GOG's Linux DOSBox depots need special-casing (see
    ``helpers.build_native_argv``) and every native title needs its
    executable bit set and Steam Overlay/Input restored — both ported
    from the previously-unreferenced ``flows/native.py``.
    """
    try:
        from unifideck.core.types.events import Events

        from .helpers import run_native_subprocess
        store = ctx.store
        game_id = ctx.game_id

        # Phase 1: Cloud Sync Down
        await svc._sync_saves_and_track_size(ctx, "sync_down")

        # Pre-launch event
        await svc._bus.emit(
            Events.GAME_LAUNCHED,
            store=store,
            game_id=game_id,
            title="",  # No title on LaunchContext
            app_id=0  # No app_id on LaunchContext
        )

        # Phase 2: Run Subprocess
        try:
            state.rc = await run_native_subprocess(svc, ctx, state)
        finally:
            await svc._bus.emit(Events.GAME_STOPPED, store=store, game_id=game_id)

        # Phase 3: Cloud Sync Up
        await svc._sync_saves_and_track_size(ctx, "sync_up")

        exit_code = svc._resolve_exit_code(state)
        # See the launch_windows path for the rationale — Result has
        # no ``rc`` field, exit codes round-trip via ``error_code``
        # (``exit_<N>`` prefix). The earlier ``rc=`` form raised
        # ``TypeError`` on every native-Linux launch.
        return Result(
            success=(exit_code == 0),
            error_code=None if exit_code == 0 else f"exit_{exit_code}",
        )

    except Exception:
        logger.exception("[Orchestrator] Native launch failed")
        raise
