"""services/launcher/helpers.py — Technical primitives for launch flows.

Functions supporting the public orchestrators
(``launch_windows`` / ``launch_native``). Most take a
``LauncherService`` as first arg (``svc``). Byte-identical
behaviour to the pre-extraction versions — split out for volumetry.

``build_native_argv``/``prepare_native_env``/``restore_steam_env``/
``find_steam_runtime`` were ported in from ``launcher/flows/native.py``
(2026-07) — that module's ``native_launch`` was never actually wired
into the live dispatch chain (``LauncherService`` calls
``orchestrator.launch_native`` instead), so its DOSBox-dispatch,
chmod, Steam Runtime wrapping, and Steam-env restore had silently
never run. Ported here rather than reconnecting the dead module, to
avoid leaving two parallel native-launch implementations again.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
    from unifideck.launcher.types.context import LaunchContext, RuntimeState

    from .service import LauncherService

logger = logging.getLogger(__name__)

_STEAM_RUNTIME_CANDIDATES = (
    "~/.steam/steam/ubuntu12_32/steam-runtime/run.sh",
    "~/.local/share/Steam/ubuntu12_32/steam-runtime/run.sh",
)


def find_steam_runtime() -> Path | None:
    """Return the Steam Runtime's ``run.sh`` wrapper, or ``None``."""
    for candidate in _STEAM_RUNTIME_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def restore_steam_env(env: dict[str, str]) -> None:
    """Restore Steam Overlay/Input/LD_PRELOAD for a native Linux launch.

    ``bin/unifideck-launcher`` unconditionally strips ``LD_PRELOAD`` at
    process start (needed for the Proton/pressure-vessel path). Native
    Linux games run directly on the host with no container involved,
    so unlike Proton there's no host/container library mismatch to
    worry about — and without Steam's overlay preload restored,
    Steam/gamescope has no in-process hook into the game to know it's
    running: Gaming Mode's loading transition never dismisses and
    Steam Input never attaches (the retired bash launcher's equivalent
    called this "critical for controller support").
    """
    steam_env = Path("~/.steam/steam.env").expanduser()
    if not steam_env.is_file():
        return
    with contextlib.suppress(OSError):
        for raw_line in steam_env.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in ("STEAM_OVERLAY", "STEAM_INPUT", "LD_PRELOAD"):
                env[key] = value


def prepare_native_env(ctx: LaunchContext) -> dict[str, str]:
    """Build the subprocess env for a native Linux launch.

    ``unifideck-launcher`` (the process this runs inside) makes the
    ``unifideck`` package importable purely via an in-process
    ``sys.path.insert`` — it never sets ``PYTHONPATH``. That's invisible
    to a *child* interpreter: when a GOG DOSBox title's argv re-invokes
    ``python3 -m unifideck.launcher.proton.handlers.gog_linux_dosbox``
    (see ``build_native_argv``), the child got a bare env with no
    ``unifideck`` on its path and died with ``ModuleNotFoundError``
    within milliseconds — silently, since nothing captured its stderr.
    Prepending ``ctx.plugin_dir / "py_modules"`` to ``PYTHONPATH`` here
    mirrors ``unifideck-launcher``'s own bootstrap so the child resolves
    the package the same way the parent did.
    """
    env = dict(os.environ)
    env.update(ctx.env_overrides)
    restore_steam_env(env)
    py_modules = str(ctx.plugin_dir / "py_modules")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{py_modules}:{existing}" if existing else py_modules
    return env


def _is_gog_dosbox_wrapper(ctx: LaunchContext) -> bool:
    """Whether ``ctx`` is a GOG Linux-depot DOSBox title (``start.sh``)."""
    return ctx.store == "gog" and ctx.exe_path.name == "start.sh"


def build_native_argv(
    ctx: LaunchContext, state: RuntimeState, exe_path: Path,
) -> list[str]:
    """Build the argv to spawn for a native Linux launch.

    GOG's Linux DOSBox depots ship a ``start.sh`` that shells out to a
    bundled DOSBox binary with the wrong (generic) invocation if exec'd
    directly — route those through the purpose-built conf-aware
    ``gog_linux_dosbox`` module instead. Every other native title is
    wrapped with the Steam Runtime when present (matches the retired
    bash launcher's behaviour), else exec'd directly.
    """
    argv: list[str] = []
    if _is_gog_dosbox_wrapper(ctx):
        logger.info("[Helpers] using GOG DOSBox wrapper module for %s", exe_path)
        # ``sys.executable`` — the interpreter already running this code
        # (whatever ``unifideck-launcher``'s shebang resolved) — not a
        # bare "python3" looked up on PATH, which can resolve to a
        # different/absent interpreter depending on the scrubbed launch
        # env. The module is stdlib-only, so no ABI-matching concern
        # like umu's cryptography/cffi dependency (see
        # ``find_python_3_10_plus``) applies here.
        argv.extend([
            sys.executable, "-m",
            "unifideck.launcher.proton.handlers.gog_linux_dosbox",
            str(exe_path),
        ])
    else:
        runtime = find_steam_runtime()
        if runtime is not None:
            logger.info("[Helpers] using Steam Runtime: %s", runtime)
            argv.extend([str(runtime), str(exe_path)])
        else:
            logger.info("[Helpers] no Steam Runtime, direct exec")
            argv.append(str(exe_path))
    argv.extend(state.game_args)
    return argv


async def run_native_subprocess(
    svc: LauncherService, ctx: LaunchContext, state: RuntimeState,
) -> int:
    """Set up and run a native Linux game's subprocess, returning its exit code.

    Chmods the executable bit (GOG's Linux depots don't always ship
    one set), builds the DOSBox-aware/Steam-Runtime-wrapped argv, and
    restores the Steam Overlay/Input env before spawning — see
    ``build_native_argv``/``prepare_native_env``.
    """
    import asyncio

    try:
        await asyncio.to_thread(ctx.exe_path.chmod, 0o755)
    except OSError as e:
        logger.debug("[Helpers] chmod 755 failed on %s: %s", ctx.exe_path, e)

    env = prepare_native_env(ctx)
    cmd = build_native_argv(ctx, state, ctx.exe_path)
    logger.info("[Helpers] Spawning native launch: %s", cmd)

    # The DOSBox wrapper is itself a Python subprocess that can fail
    # before ever exec'ing the real game (bad PYTHONPATH, missing
    # module) — capture its stderr so that class of failure is logged
    # instead of vanishing silently, as it did the first time this path
    # actually ran (see ``prepare_native_env``'s PYTHONPATH fix). Every
    # other native title inherits the parent's stdout/stderr directly,
    # unchanged, since some expect a real terminal (console output,
    # audio subsystem probes).
    capture_stderr = _is_gog_dosbox_wrapper(ctx)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ctx.work_dir),
        env=env,
        stderr=asyncio.subprocess.PIPE if capture_stderr else None,
    )
    svc._active_subprocess = proc
    try:
        if capture_stderr:
            _, stderr = await proc.communicate()
            if proc.returncode != 0 and stderr:
                logger.warning(
                    "[Helpers] DOSBox wrapper exited %s: %s",
                    proc.returncode, stderr.decode(errors="replace").strip(),
                )
            return proc.returncode or 0
        return await proc.wait()
    finally:
        svc._active_subprocess = None


@functools.lru_cache(maxsize=1)
def _cffi_backend_available() -> bool:
    """Whether the host's system Python can import cffi's native backend.

    The out-of-process launcher runs under the host ``/usr/bin/python3``,
    whose minor version varies across distros (SteamOS/Bazzite/CachyOS).
    The cloud-save service imports cryptography → cffi → ``_cffi_backend``,
    an ABI-specific ``.so`` we vendor per Python version at build time. When
    the host Python has no matching backend, the cloud-save service fails to
    instantiate and ``svc._cloud_svc`` is ``None`` — this probe lets us tell
    that specific cause apart so the user gets an actionable toast instead of
    silence. Cached: the answer can't change within a process.
    """
    try:
        import _cffi_backend  # type: ignore[import-untyped]  # noqa: F401
    except Exception:
        return False
    return True


async def _emit_cloud_unavailable_toast(
    svc: LauncherService, ctx: LaunchContext,
) -> None:
    """Warn once that cloud-save is off because of an unsupported host Python.

    Best-effort and fully isolated — any failure here is swallowed so it can
    never affect the launch.
    """
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    logger.warning(
        "[Helpers] cloud-save disabled: system Python %s has no matching "
        "cffi backend (vendor it in build-plugin.sh LAUNCHER_PYTHON_VERSIONS)",
        py_ver,
    )
    try:
        from unifideck.launcher.rpc import emit_stage

        await emit_stage(
            svc._bus,
            i18n_key="cloudSync.unavailableNativeDep",
            game_title=getattr(ctx, "game_key", None)
            or f"{ctx.store}:{ctx.game_id}",
            priority="low",
            severity="warning",
            i18n_params={"python": py_ver},
        )
    except Exception as e:  # never let a toast break a launch
        logger.debug("[Helpers] cloud-unavailable toast failed: %s", e)


def _resolve_launch_proton(
    ctx: LaunchContext,
    tool_id: str | None,
    select_proton_version: Any,
) -> tuple[Any, str]:
    """The ``(path, tool_id)`` to build the launch plan with.

    ``tool_id`` pins an explicit Proton — used to rebuild the plan after
    ``setup_prefix`` concluded a different one is the working Proton, so the
    game runs under whatever actually built its prefix. Falls back to normal
    resolution if that id no longer resolves to a path.
    """
    from unifideck.launcher.proton import resolve_proton_path

    if tool_id:
        path = resolve_proton_path(tool_id)
        if path is not None:
            return path, tool_id
        logger.warning(
            "[Helpers] proton=%s did not resolve to a path — falling back to "
            "normal selection for %s", tool_id, ctx.game_key,
        )
    resolved: tuple[Any, str] = select_proton_version(
        steam_app_id=ctx.steam_app_id,
        store_game_id=ctx.game_key,
    )
    return resolved


async def prepare_windows_plan(
    svc: LauncherService,
    ctx: LaunchContext,
    state: RuntimeState,
    *,
    tool_id: str | None = None,
) -> ProtonLaunchPlan:
    """Prepare the Proton launch plan for a Windows game.

    Resolves the three things ``proton_prepare`` needs — a
    Python 3.10+ interpreter, the Proton tool path, and its
    tool id — then builds the immutable ``ProtonLaunchPlan``
    the store handlers consume. Proton is selected by
    ``select_proton_version``, which honours (in order) the
    per-game tool the frontend captured into
    ``proton_settings.json``, any Steam compat override, the
    Unifideck default, and finally a GE-Proton fallback.

    The ``on_process_start`` callback registers the spawned
    process on the service so SIGTERM/SIGINT cancellation can
    reach it (mirrors the native path's ``_active_subprocess``).
    """
    from unifideck.launcher.proton import (
        find_python_3_10_plus,
        proton_prepare,
        select_proton_version,
    )

    try:
        python_bin = find_python_3_10_plus()
        proton_path, proton_tool_id = _resolve_launch_proton(
            ctx, tool_id, select_proton_version,
        )
        def _on_process_start(proc: object) -> None:
            svc._active_subprocess = proc

        # ``proton_prepare`` is synchronous (prefix mkdir + umu-id
        # lookup); call it directly — the launcher subprocess has
        # nothing else on its event loop.
        return proton_prepare(
            ctx,
            state,
            python_bin=python_bin,
            proton_path=proton_path,
            proton_tool_id=proton_tool_id,
            on_process_start=_on_process_start,
        )
    except Exception:
        logger.exception("[Helpers] prepare_windows_plan failed")
        raise


async def cloud_sync_phase(
    svc: LauncherService,
    ctx: LaunchContext,
    direction: str,
) -> None:
    """Run one direction of cloud-save sync (``down`` or ``up``)."""
    store = ctx.store
    game_id = ctx.game_id

    if not store or not game_id:
        return

    # Cloud-save is optional: the launcher may have been built without it
    # (e.g. the service failed to instantiate). A launch must never depend
    # on cloud-save being present, so skip when it's unavailable.
    if svc._cloud_svc is None:
        logger.debug(
            "[Helpers] Cloud sync %s skipped — cloud service unavailable",
            direction,
        )
        # If the cause is a host Python with no matching cffi backend
        # (the common non-SteamOS failure mode), tell the user once —
        # on the "down" phase, which always runs first — instead of
        # leaving them to wonder why saves never sync. Best-effort:
        # the toast must never block or fail a launch.
        if direction == "down" and not _cffi_backend_available():
            await _emit_cloud_unavailable_toast(svc, ctx)
        return

    # Respect the auto-sync config flags. Download-on-launch is on by default;
    # upload-on-stop is OFF by default (manual via the cloud-save button), so
    # this is the path that must honour ``cloud.auto_push_on_stop``.
    if hasattr(svc._cloud_svc, "auto_sync_enabled") and not svc._cloud_svc.auto_sync_enabled(direction):
        logger.info(
            "[Helpers] Cloud sync %s skipped — disabled by config", direction,
        )
        return

    try:
        if direction == "down":
            await svc._cloud_svc.sync_down(store, game_id)
        elif direction == "up":
            await svc._cloud_svc.sync_up(store, game_id)
    except Exception as e:
        # Surface it. This used to be a bare ``logger.warning(... ignoring ...)``,
        # so a failed upload was **completely silent**: the user quit the game
        # believing their progress had gone to the cloud, and nothing said
        # otherwise until they lost it on another device.
        #
        # ``cloud_failure`` was written for exactly this and had no caller —
        # its whole module was unimported (audit register item 37). Audit §1.2
        # then read its config lookup as live and, on that basis, deleted the
        # two RPCs that would have let a user silence the toast, reasoning
        # that "current behaviour is acceptable". There was no behaviour: the
        # toast never fired. Surfacing it is the 2026-08-26 product decision.
        #
        # Best-effort by design — a toast must never turn a survivable sync
        # failure into a failed launch, which is why the whole thing is
        # wrapped again here.
        logger.warning("[Helpers] Cloud sync %s failed: %s", direction, e)
        try:
            from unifideck.launcher.cloud.cloud_failure import (
                handle_cloud_sync_failure,
            )

            await handle_cloud_sync_failure(
                svc._bus,
                getattr(svc, "_config", None),
                phase=f"sync_{direction}",
                store=store,
                game_id=game_id,
                error=e,
            )
        except Exception:
            logger.exception(
                "[Helpers] cloud-failure reporting itself failed for %s",
                direction,
            )


async def run_game_subprocess(
    svc: LauncherService,
    plan: Any,
    ctx: LaunchContext,
    state: RuntimeState,
) -> int:
    """Run the Windows game via the per-store Proton handler.

    Delegates to ``proton.dispatch`` which routes the
    ``ProtonLaunchPlan`` to the right store handler
    (epic / ubisoft / generic) and runs it through umu-run.
    The spawned process is registered on the service via the
    plan's ``on_process_start`` callback (wired in
    ``prepare_windows_plan``), so cancellation can reach it;
    we clear the reference once the handler returns.
    """
    from unifideck.launcher.proton import dispatch

    # Report PROTONPATH, not ``state.proton_tool_id``: the state object is
    # shared and ``setup_prefix`` mutates it while borrowing another Proton for
    # the winetricks verb, so the tool id could name a Proton this launch never
    # uses. PROTONPATH is what umu actually reads, so this line cannot drift
    # from reality again (it previously logged "GE-Proton11-3" for launches umu
    # ran under Proton-Experimental).
    logger.info(
        "[Helpers] Dispatching Proton launch: store=%s game_id=%s proton=%s",
        ctx.store, ctx.game_id, Path(plan.env["PROTONPATH"]).name,
    )
    try:
        rc = await dispatch(plan)
    finally:
        svc._active_subprocess = None

    return rc


async def sync_saves_and_track_size(
    svc: LauncherService,
    ctx: LaunchContext,
    phase: str,
) -> None:
    """Run cloud sync for native games."""
    # Simplified equivalent wrapper for native sync calls
    direction = "down" if "down" in phase else "up"
    await cloud_sync_phase(svc, ctx, direction)


def resolve_exit_code(svc: LauncherService, state: RuntimeState) -> int:
    """Resolve the final exit code."""
    if getattr(svc, "_cancelled", False):
        return -1
    return getattr(state, "rc", 1)


def elapsed_since_launch(svc: LauncherService) -> float:
    """Return time elapsed since launch started."""
    if svc._launch_started_at is None:
        return 0.0
    return time.monotonic() - svc._launch_started_at
