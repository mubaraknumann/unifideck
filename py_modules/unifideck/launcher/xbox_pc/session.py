"""launcher/xbox_pc/session.py — one launch of an installed Xbox PC game.

``XboxPcLaunch.run()`` is the whole launch, in five steps:

1. ``_preflight``: cheap refusals before anything is locked or started
   (no Steam root, no install record, not fully installed, no market, not
   signed in).
2. ``_acquire``: the game lock (a second launch of the same game refuses
   with ``already_running``), the runtime and backend held shared so an
   update cannot delete them mid-game, and the component checks. A runtime
   without cloud saves refuses (CLAUDE.md rule 8): the Xodus cloud is the
   only real save sync for these games.
3. ``_prepare``: the game manifest, the sweep of stale launch views, the
   prefix, the runner, the VC++ and EasyAntiCheat checks, the language.
4. ``_spawn``: ``xodus-service`` and its bridge, the Gaming Mode window
   tagger, then ``xodus-cli run``.
5. ``_supervise`` waits for it. A cancel (Steam's Stop) runs the staged
   stop; a second cancel forces it. ``_teardown`` always runs.

Anything the launcher process alone cannot resolve (the Steam root and
libraries, the window env, the UI language, the game log, toasts) comes in
through :class:`LaunchDeps`, so this package stays stdlib only. The
service layer builds it (``services/launcher/xbox_pc_game.py``).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from unifideck.launcher.proton.infrastructure.prefix_layout import game_prefix_root
from unifideck.launcher.types.context import LaunchContext

from .argv import build_env, run_argv, validate_market
from .backend import BackendPaths
from .component_locks import ComponentsBusyError, game_lock, reading_components
from .environment import build_game_env, clean_env, pick_locale
from .errors import LANGUAGE_UNAVAILABLE_KEY, XboxPcLaunchError
from .game_manifest import GameInfo, inspect_game
from .layout import ActiveComponents, credential_dir, validate_pid
from .output import OutputSink, classify_failure
from .prefix import prepare_prefix
from .prefix_checks import installed_eac_runtime, vc_runtime_problem
from .record_lookup import find_install_record
from .runner import write_runner
from .runtime import Runtime, load_backend, load_runtime
from .service_session import ServiceSession, sessions_dir
from .stop import StagedStopper, kill_and_reap, stop_gracefully
from .sweep import sweep_launch_views, sweep_sessions

logger = logging.getLogger(__name__)

Notify = Callable[[str, Mapping[str, Any]], Awaitable[None]]

#: The credential file a signed-in account always has.
USER_SESSION_FILE = "user-session"
_PUMP_DRAIN_S = 5.0
_INITIALIZING_PREFIX_KEY = "toasts.launcher.initializingPrefix"


@dataclass(frozen=True)
class LaunchDeps:
    """What the launch needs from outside this package.

    ``window_env`` is the Steam identity block, or None when the shortcut's
    app id is unknown (the inherited ``SteamGameId`` is then kept).
    ``notify`` shows a non-error notice; ``on_spawned`` runs once the game
    process exists.
    """

    steam_root: Path | None
    steam_libraries: tuple[Path, ...] = ()
    window_env: Mapping[str, str] | None = None
    language: str | None = None
    log_file: IO[str] | None = None
    fast_fail_s: float = 10.0
    base_env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    notify: Notify | None = None
    on_spawned: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True)
class _Plan:
    """The checked inputs of one launch."""

    pid: str
    market: str
    game_dir: Path
    steam_root: Path
    credentials: Path


class XboxPcLaunch:
    """Runs one installed Xbox PC game and returns its exit code."""

    def __init__(self, ctx: LaunchContext, deps: LaunchDeps) -> None:
        """Nothing starts until :meth:`run`."""
        self._ctx = ctx
        self._deps = deps
        self._sink = OutputSink(deps.log_file)
        self._lock_held = False
        self._runtime: Runtime | None = None
        self._backend: BackendPaths | None = None
        self._game: GameInfo | None = None
        self._runner: Path | None = None
        self._locale: str | None = None
        self._service: ServiceSession | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Future[None] | None = None
        self._tag_stop: Any = None
        self._spawned_at: float | None = None

    async def run(self) -> int:
        """Launch, wait for the game, clean up. Raises :class:`XboxPcLaunchError`."""
        plan = self._preflight()
        with contextlib.ExitStack() as stack:
            try:
                await self._acquire(plan, stack)
                await self._prepare(plan)
                await self._spawn(plan)
                rc = await self._supervise()
            finally:
                await _run_to_completion(self._teardown(plan))
        # Measured from the xodus-cli spawn. Prefix creation and the service
        # start can take minutes and must not hide a fast refusal.
        spawned_at = self._spawned_at if self._spawned_at is not None else time.monotonic()
        return self._judge_exit(rc, time.monotonic() - spawned_at)

    def _judge_exit(self, rc: int, elapsed: float) -> int:
        """``rc``, or the classified refusal when the run died at once."""
        logger.info("[xbox_pc] xodus-cli exited %d after %.0fs", rc, elapsed)
        if rc != 0 and elapsed < self._deps.fast_fail_s:
            raise XboxPcLaunchError(classify_failure(self._sink.tail()), f"exit {rc}")
        return rc

    # ── 1. preflight ─────────────────────────────────────────────────

    def _preflight(self) -> _Plan:
        """The cheap refusals. Nothing is locked or started yet.

        The record's ``productId`` names the lock, the prefix and the
        session folders. ``ctx.game_id`` is the row id, which may differ.
        """
        steam_root = self._deps.steam_root
        if steam_root is None:
            raise XboxPcLaunchError("steam_root_missing", "find_steam_root() found none")
        record = find_install_record(self._ctx.game_id)
        if record is None or record.get("state") != "installed":
            state = record.get("state") if record else "no record"
            raise XboxPcLaunchError("not_downloaded", f"install state: {state}")
        game_dir = str(record.get("gameDir") or "")
        if not os.path.isabs(game_dir):
            raise XboxPcLaunchError("not_downloaded", "the record has no absolute gameDir")
        try:
            market = validate_market(str(record.get("market") or ""))
        except ValueError as err:
            raise XboxPcLaunchError("market_unknown", str(err)) from err
        credentials = credential_dir()
        if not (credentials / USER_SESSION_FILE).is_file():
            raise XboxPcLaunchError("sign_in_required", "no stored Xbox PC session")
        pid = validate_pid(str(record["productId"]))
        return _Plan(pid, market, Path(game_dir), steam_root, credentials)

    # ── 2. locks and components ──────────────────────────────────────

    async def _acquire(self, plan: _Plan, stack: contextlib.ExitStack) -> None:
        """Hold the game and component locks on ``stack`` and check the components."""
        try:
            self._lock_held = stack.enter_context(game_lock(plan.pid))
        except OSError as err:
            raise XboxPcLaunchError("launch_failed", f"game lock: {err}") from err
        if not self._lock_held:
            raise XboxPcLaunchError("already_running", plan.pid)
        try:
            active = await _hold_components(stack)
        except ComponentsBusyError as err:
            raise XboxPcLaunchError("components_busy", str(err)) from err
        if active is None:
            raise XboxPcLaunchError("runtime_missing", "no active.json")
        runtime = await asyncio.to_thread(load_runtime, active.runtime_dir)
        self._backend = await asyncio.to_thread(load_backend, active)
        if runtime.unbridged_cloud_saves:
            raise XboxPcLaunchError("runtime_outdated", "cloud saves without the bridge")
        if not runtime.cloud_saves:
            raise XboxPcLaunchError("cloud_save_unavailable", f"features: {runtime.features}")
        self._runtime = runtime
        logger.info(
            "[xbox_pc] runtime %s, backend %s", runtime.root.name, self._backend.version,
        )

    # ── 3. game, prefix, runner ──────────────────────────────────────

    async def _prepare(self, plan: _Plan) -> None:
        """The game, the sweeps, the prefix, the runner and the checks."""
        game = await asyncio.to_thread(inspect_game, plan.game_dir)
        if not game.container_ready:
            raise XboxPcLaunchError("not_downloaded", "no .xodus-streaming.msixvc")
        self._game = game
        logger.info(
            "[xbox_pc] game %s (%s), exe %s, eac %s",
            game.title, game.store_id or plan.pid, game.executable, game.eac or "none",
        )
        await asyncio.to_thread(self._sweep, plan)
        compat_root = game_prefix_root(plan.pid)
        await self._prepare_prefix(plan, compat_root)
        if game.vc_redist_encrypted:
            problem = await asyncio.to_thread(vc_runtime_problem, compat_root)
            if problem:
                raise XboxPcLaunchError("vcredist_missing", problem)
        await self._choose_locale(game)

    def _sweep(self, plan: _Plan) -> None:
        """Remove this game's stale launch views and service folders."""
        sweep_launch_views(plan.game_dir, lock_held=self._lock_held)
        sweep_sessions(sessions_dir(), plan.pid, lock_held=self._lock_held)

    async def _prepare_prefix(self, plan: _Plan, compat_root: Path) -> None:
        """Make the prefix ready and write the runner into it."""
        runtime, game = self._runtime, self._game
        assert runtime is not None and game is not None
        await prepare_prefix(
            runtime, compat_root, plan.steam_root,
            base_env=self._deps.base_env, on_create=self._notify_prefix,
        )
        eac = installed_eac_runtime(self._deps.steam_libraries)
        if game.eac and eac is None:
            raise XboxPcLaunchError("eac_runtime_missing", game.eac)
        self._runner = await asyncio.to_thread(
            write_runner, runtime, compat_root, eac_runtime=eac,
        )

    async def _notify_prefix(self) -> None:
        """Tell the user a first launch is creating the prefix."""
        await self._notify(_INITIALIZING_PREFIX_KEY, {})

    async def _choose_locale(self, game: GameInfo) -> None:
        """Run the game in the UI language when it declares it; say so when not."""
        requested = self._deps.language
        self._locale = pick_locale(requested, game.locales)
        if requested and game.locales and self._locale is None:
            logger.info(
                "[xbox_pc] %s is not declared (%s); the game picks its own",
                requested, ", ".join(game.locales),
            )
            await self._notify(LANGUAGE_UNAVAILABLE_KEY, {"language": requested})

    async def _notify(self, key: str, params: Mapping[str, Any]) -> None:
        """A non-error notice, best effort."""
        if self._deps.notify is None:
            return
        try:
            await self._deps.notify(key, params)
        except Exception as err:
            logger.warning("[xbox_pc] notice %s failed: %s", key, err)

    # ── 4. spawn ─────────────────────────────────────────────────────

    async def _spawn(self, plan: _Plan) -> None:
        """Start the service, the tagger and ``xodus-cli run``."""
        from unifideck.launcher.proton.infrastructure.gamescope_window_tagger import (
            maybe_start_window_tagger,
        )

        backend, game, runner = self._backend, self._game, self._runner
        assert backend is not None and game is not None and runner is not None
        base = self._deps.base_env
        self._service = ServiceSession(
            backend.service, build_env(clean_env(base), plan.credentials), self._sink, pid=plan.pid,
        )
        await self._service.start()
        env = build_game_env(
            base, credential_dir=plan.credentials, locale=self._locale,
            window_env=self._deps.window_env, service_env=self._service.env(),
            overrides=self._ctx.env_overrides,
        )
        self._tag_stop = maybe_start_window_tagger(env, log_tag="xbox_pc")
        argv = run_argv(backend.cli, game.path, runner, game.executable, plan.market)
        logger.info("[xbox_pc] running xodus-cli run <game> <runner> --exe %s", game.executable)
        await self._start_cli(argv, env, game.path)

    async def _start_cli(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        """Start ``xodus-cli run`` with its output pumped into the sink."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as err:
            raise XboxPcLaunchError("launch_failed", f"xodus-cli did not start: {err}") from err
        self._spawned_at = time.monotonic()
        self._pump = asyncio.ensure_future(self._sink.pump(self._proc.stdout, source="game"))
        if self._deps.on_spawned is not None:
            await self._deps.on_spawned()

    # ── 5. supervise and teardown ────────────────────────────────────

    async def _supervise(self) -> int:
        """Wait for ``xodus-cli``. A cancel stops the game in stages."""
        proc, game, runner = self._proc, self._game, self._runner
        assert proc is not None and game is not None and runner is not None
        try:
            return await proc.wait()
        except asyncio.CancelledError:
            logger.info("[xbox_pc] stop requested; closing the game")
            stopper = StagedStopper(
                proc, runner=runner, exe_name=game.exe_name, env=clean_env(self._deps.base_env),
            )
            await stop_gracefully(stopper)

    async def _teardown(self, plan: _Plan) -> None:
        """Stop everything this launch started. Never raises.

        ``run`` shields it, so a repeated Stop cannot cut it short and leave
        ``xodus-service`` running with the credentials loaded.
        """
        if self._tag_stop is not None:
            self._tag_stop.set()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await kill_and_reap(proc)
        if self._pump is not None:
            await asyncio.wait({self._pump}, timeout=_PUMP_DRAIN_S)
            if not self._pump.done():
                self._pump.cancel()
        if self._service is not None:
            try:
                await self._service.stop()
            except Exception as err:
                logger.warning("[xbox_pc] service stop failed: %s", err)
        if self._lock_held and self._game is not None:
            try:
                await asyncio.to_thread(self._sweep, plan)
            except Exception as err:
                logger.warning("[xbox_pc] teardown sweep failed: %s", err)


async def _run_to_completion(work: Awaitable[None]) -> None:
    """Await ``work`` to the end even when the caller is cancelled meanwhile.

    Steam re-sends SIGTERM and each one cancels the launch task. A cancel
    seen here is kept and raised again once ``work`` has finished.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


def _enter_components() -> tuple[contextlib.ExitStack, ActiveComponents | None]:
    """Take the component locks on a fresh stack (runs in a worker thread)."""
    local = contextlib.ExitStack()
    try:
        active = local.enter_context(reading_components())
    except BaseException:
        local.close()
        raise
    return local, active


def _close_late(future: asyncio.Future[tuple[contextlib.ExitStack, Any]]) -> None:
    """Release locks a worker thread took after its caller was cancelled."""
    if future.cancelled() or future.exception() is not None:
        return
    future.result()[0].close()


async def _hold_components(stack: contextlib.ExitStack) -> ActiveComponents | None:
    """Take the component locks in a thread and move them onto ``stack``.

    The shared lock wait can last seconds. A cancel meanwhile leaves the
    thread running, so the locks it takes then are released when it ends
    instead of landing on a stack that is already closed.
    """
    future = asyncio.ensure_future(asyncio.to_thread(_enter_components))
    try:
        local, active = await asyncio.shield(future)
    except asyncio.CancelledError:
        future.add_done_callback(_close_late)
        raise
    stack.enter_context(local)
    return active
