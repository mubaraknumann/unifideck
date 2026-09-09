"""services/launcher/service.py — LauncherService DI facade.

Single entry point used by main.py and the dispatcher CLI. Holds
references to existing services (ShortcutService, ProtonService,
CloudSaveService, EdgeBrowser) and orchestrates a single launch
end-to-end. No logic duplication — all non-trivial work is
delegated. The remaining code here is dispatch + signal wiring +
launch stage events + CLI-tool subprocess wrapping.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Result
from unifideck.launcher.rpc import emit_stage
from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.launcher.types.options import parse_launch_options
from unifideck.launcher.wrapper_stores import is_wrapper_store

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
    from unifideck.services.cloud_save.service import CloudSaveService
    from unifideck.services.proton_service import ProtonService
    from unifideck.services.shortcut.service import ShortcutService

logger = logging.getLogger(__name__)


class LauncherService:
    """Facade orchestrating one launch via delegation to services."""

    def __init__(
        self,
        bus: EventBus,
        shortcut_svc: ShortcutService,
        proton_svc: ProtonService,
        cloud_svc: CloudSaveService | None,
        edge_browser: EdgeBrowser,
        config: Any | None = None,
        launch_history: Any | None = None,
    ) -> None:
        """Store injected deps + initialise signal/process registry state."""
        self._bus = bus
        self._shortcut_svc = shortcut_svc
        self._proton_svc = proton_svc
        self._cloud_svc = cloud_svc
        self._edge_browser = edge_browser
        self._config = config
        self._launch_history = launch_history

        self._active_subprocess: Any = None
        self._cancelled = False
        self._launch_started_at: float | None = None
        self._launch_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        """Install signal handlers. Called by ServiceBootstrap.

        ``SIGTERM``/``SIGINT`` (Steam's Stop button, the Play-UI X) cancel
        the in-flight launch **task**, which unwinds into
        ``umu_runtime._run_umu_once``'s ``except asyncio.CancelledError``
        and reaps umu-run's whole process group plus the prefix's
        wineserver. Cancelling beats the previous single
        ``_active_subprocess.terminate()``: umu-run runs with
        ``start_new_session=True``, so one SIGTERM racing Steam's SIGKILL
        was the only thing standing between "Stop" and a game that keeps
        running. ``CancelledError`` also bypasses ``handle_launcher_error``,
        so a user-initiated stop no longer raises a bogus "launcher error"
        toast or feeds the circuit breaker.

        ``add_signal_handler`` is preferred (the callback runs on the loop,
        not between bytecodes); the old handler stays as the fallback for
        any context without a running loop. Only ever installed in the
        launcher subprocess — ``LauncherService`` is built solely by the
        CLI dispatcher's bootstrap, never by the plugin backend.
        """
        def _cancel_launch(sig: int) -> None:
            logger.info("[LauncherService] received signal %s, cancelling launch", sig)
            self._cancelled = True
            task = self._launch_task
            if task is not None and not task.done():
                task.cancel()
                return
            if self._active_subprocess:
                try:
                    self._active_subprocess.terminate()
                except Exception as e:
                    logger.debug("[LauncherService] terminate failed: %s", e)

        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, _cancel_launch, sig)
            return
        except (NotImplementedError, RuntimeError) as e:
            logger.debug("[LauncherService] loop signal install failed: %s", e)

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda s, _f: _cancel_launch(s))
        except ValueError:
            # We might not be in the main thread
            pass
        except Exception as e:
            logger.debug("[LauncherService] signal install failed: %s", e)

    async def stop(self) -> None:
        """Bootstrap teardown hook. No-op for now — signals are
        removed when the event loop shuts down.
        """

    async def launch(self, ctx: LaunchContext) -> Result:
        """Launch a game described by the immutable ``LaunchContext``.

        Dispatch matrix: xCloud → ``_launch_xcloud``; Windows →
        ``_launch_windows``; native Linux → ``_launch_native``.
        Wrapped in circuit-breaker check + error-toast emission.
        Returns a ``Result`` summarising exit code + elapsed time.

        Refactor history (lot 13a, FANOUT=10): the body delegates
        to five private helpers (``_start_launch_clock``,
        ``_circuit_open_result``, ``_build_runtime_state``,
        ``_try_launch``, ``_handle_launcher_error``) so this
        method stays well under the fan-out gate. Earlier the
        method called 12+ distinct symbols at the top level.
        """
        self._start_launch_clock()
        if await self._check_circuit_breaker(ctx):
            return self._circuit_open_result()
        state = self._build_runtime_state(ctx)
        # The task a Stop/SIGTERM cancels — see ``start``. Recorded here
        # rather than in ``start`` so only a launch in flight is ever
        # cancellable.
        self._launch_task = asyncio.current_task()
        try:
            return await self._try_launch(ctx, state)
        except Exception as e:
            return await self._handle_launcher_error(ctx, e)
        finally:
            self._launch_task = None

    def _start_launch_clock(self) -> None:
        """Record the launch start time used by ``_elapsed_since_launch``.

        Extracted so ``launch`` doesn't reference ``time.monotonic``
        directly, which would count against the fan-out gate.
        """
        import time
        self._launch_started_at = time.monotonic()

    @staticmethod
    def _circuit_open_result() -> Result:
        """Build the canonical 'circuit breaker open' failure result.

        One-liner extracted from ``launch`` so the ``Result``
        constructor call doesn't inflate that method's fan-out.
        """
        # ``error_code`` as well as ``error``: ``_map_result_to_exitcode``
        # dispatches on ``error_code`` **exclusively**, so leaving it None
        # collapsed every classified failure to GAME_FAILED (8) and made the
        # CIRCUIT_BREAKER_OPEN branch dead. Audit register item 4c.
        return Result(
            success=False, error="circuit_open", error_code="circuit_open",
        )

    async def _try_launch(
        self, ctx: LaunchContext, state: RuntimeState,
    ) -> Result:
        """Run the main launch pipeline inside the error-trap.

        Three steps:

        1. Route to the non-launch handler (auth / storefront /
           install) or to the launch dispatch matrix (real launches).
        2. For a real launch, emit the ``launchingGame`` toast.
        3. Enrich the launch result with the elapsed time, stashed
           in ``Result.metadata["elapsed"]`` because ``Result``
           has no dedicated elapsed field and the frontend reads
           this via the documented metadata channel.

        The non-launch branch returns BEFORE the toast: a sign-in or a
        shop window is not a game, and announcing "Launching Game:
        epic:epic-store" for one is simply wrong.

        Extracted from ``launch`` (lot 13a) so the outer method
        only calls 5 distinct symbols, well under the fan-out gate.
        """
        if not ctx.is_launch_action:
            return await self._handle_auth_path(ctx)
        await emit_stage(
            self._bus,
            i18n_key="toasts.launcher.launchingGame",
            game_title=ctx.game_key,
            priority="low",
        )
        res = await self._dispatch_launch_kind(ctx, state)
        if res.metadata is None:
            res.metadata = {}  # type: ignore[unreachable]  # guard 'if res.metadata is None'
        res.metadata["elapsed"] = self._elapsed_since_launch()
        return res

    @staticmethod
    def _build_runtime_state(ctx: LaunchContext) -> RuntimeState:
        """Construct the mutable runtime state from the user's launch options.

        Only ``lsfg_requested`` is taken from the parse. The env half is
        applied at context-construction time instead
        (``dispatcher._env_overrides_from``), because ``LaunchContext`` is
        frozen.

        ``game_args`` is populated since 2026-08-26 (audit register item
        23a); ``wrappers`` no longer exists (item 23b).

        Both halves were blocked on the same measurement. ``parse_launch_
        options`` was written for a full Steam ``LaunchOptions`` string,
        where ``%command%`` marks the boundary between wrapper words and
        game arguments — but what reaches the dispatcher is the
        *post-expansion argv tail*, which usually has no marker left. Wiring
        ``game_args`` from it used to append the user's own
        ``mangohud gamemoderun`` to the **game's** command line, because the
        frontend's ``extractUserParams`` preserved those words into the
        temp-shortcut options.

        Two changes made it safe, in this order:

        * ``extractUserParams`` now keeps only ``KEY=value`` assignments. A
          bare word after the game key was never a wrapper — Steam applies
          wrappers pre-exec, before ``%command%`` — so preserving it achieved
          nothing and was the sole source of the hazard.
        * ``wrappers`` is gone, and the parser now **drops** tokens before a
          ``%command%`` rather than re-homing them into ``game_args``.

        What remains is the honest reading: a bare token in the tail is a
        game argument, which is what Steam delivers it as.

        Extracted from ``launch`` (lot 13a) to keep that
        method's fan-out under the gate.
        """
        # Not ``started_at``: nothing has ever populated it (the dispatcher
        # never passed one, despite an older docstring here claiming it did)
        # and nothing reads it. Elapsed time comes from
        # ``self._launch_started_at``, set in ``launch`` off
        # ``time.monotonic``. Reading a user-controlled dict for an internal
        # timestamp would be a trap now that the dict is populated.
        parsed = parse_launch_options(ctx.raw_options)
        return RuntimeState(
            lsfg_requested=parsed.lsfg_requested,
            game_args=parsed.game_args,
        )

    async def _handle_auth_path(self, ctx: LaunchContext) -> Result:
        """Route a non-launch context to the right handler.

        The four OAuth stores open Edge — on a captured auth URL
        (``launcher/flows/auth.py``) or on a shop URL
        (``launcher/flows/storefront.py``). Ubisoft is different: it has
        no browser OAuth — the user signs in inside the Ubisoft Connect
        (UPC) desktop client, which must be launched via Proton in the
        ``.upc-auth`` prefix. ``handle_store_auth`` only no-ops for
        Ubisoft (it returns immediately, which is why the shortcut
        closed at once), so Ubisoft gets its own Proton path here.

        **The wrapper check must stay first.** A wrapper store has no
        browser session at all, so its shop is the vendor client's own
        Store tab, in the auth prefix. Testing ``action`` first would
        send a Ubisoft cart press to Edge and a signed-out web page.

        Extracted from ``launch`` (lot 13a) to keep that method's
        fan-out under the gate.
        """
        if is_wrapper_store(ctx.auth_store):
            return await self._launch_wrapper_client(ctx)
        if ctx.action == "storefront":
            from unifideck.launcher.flows.storefront import (
                handle_store_storefront,
            )
            return await handle_store_storefront(ctx, self._edge_browser)
        from unifideck.launcher.flows.auth import handle_store_auth
        return await handle_store_auth(ctx, self._edge_browser)

    async def _launch_wrapper_client(self, ctx: LaunchContext) -> Result:
        """Open a wrapper store's vendor client, for sign-in or install.

        Wrapper stores have no browser OAuth: the user signs in inside the
        vendor client, which must run through Proton *inside the
        RunGame-launched gamescope session* or its window never renders in
        Gaming Mode.

        Success is reported on a clean exit regardless of return code. The
        real signals live elsewhere — the session monitor observes captured
        credentials, and the download worker watches the prefix for
        installed files.
        """
        from unifideck.services.launcher.helpers import prepare_windows_plan

        handler = self._wrapper_handler(ctx.auth_store, ctx.action)
        if handler is None:
            return Result(
                success=False,
                store=ctx.auth_store,
                error=f"no wrapper handler for {ctx.auth_store}/{ctx.action}",
            )
        state = self._build_runtime_state(ctx)
        try:
            plan = await prepare_windows_plan(self, ctx, state)
            rc = await handler(plan)
        finally:
            self._active_subprocess = None
        return Result(
            success=True,
            store=ctx.auth_store,
            metadata={
                "elapsed": self._elapsed_since_launch(),
                "rc": str(rc),
            },
        )

    @staticmethod
    def _wrapper_handler(store: str | None, action: str | None) -> Any:
        """The launcher handler for one wrapper store's non-launch run.

        Only ``install`` gets its own handler. Everything else — ``auth``
        and ``storefront`` — opens the client bare in the auth prefix,
        which is both where the user signs in and where the client's own
        Store/Shop tab is already signed in. So ``storefront`` falls into
        the ``else`` on purpose; it is not an oversight.
        """
        if store == "ubisoft":
            from unifideck.launcher.proton.handlers.ubisoft import (
                ubisoft_auth_launch,
                ubisoft_install_launch,
            )

            return ubisoft_install_launch if action == "install" else ubisoft_auth_launch
        if store == "battlenet":
            from unifideck.launcher.proton.handlers.battlenet import (
                battlenet_auth_launch,
                battlenet_install_launch,
            )

            return (
                battlenet_install_launch if action == "install" else battlenet_auth_launch
            )
        return None

    async def _dispatch_launch_kind(
        self, ctx: LaunchContext, state: RuntimeState,
    ) -> Result:
        """Select the launch backend for ``ctx`` (xCloud / Windows / native).

        Pure dispatch: each branch is one async call. Extracted
        from ``launch`` (lot 13a) to keep that method's fan-out
        under the gate; the dispatch matrix itself stays trivial
        so any new launch kind only adds one entry here.
        """
        if ctx.is_xcloud:
            return await self._launch_xcloud(ctx)
        if ctx.is_windows_game:
            return await self._launch_windows(ctx, state)
        return await self._launch_native(ctx, state)

    async def _xcloud_edge_check(self, ctx: LaunchContext) -> Result | None:
        """Abort result when Edge isn't installed, else ``None`` to continue.

        xCloud streaming requires Edge. Checked before GAME_LAUNCHED so we
        don't emit a launch/stop pair for a no-op. Extracted from
        ``_launch_xcloud`` to keep that method under the line cap.
        """
        if self._edge_browser.is_installed:
            return None
        logger.warning(
            "[LauncherService] xCloud launch aborted — Edge not installed",
        )
        await emit_stage(
            self._bus,
            i18n_key="toasts.launcher.browserRequired",
            game_title=ctx.game_key,
            severity="error",
            priority="normal",
        )
        return Result(
            success=False, error="edge_not_installed", store=ctx.store,
        )

    async def _launch_xcloud(self, ctx: LaunchContext) -> Result:
        """xCloud streaming path — Edge kiosk mode on the Xbox URL."""
        from unifideck.core.types.events import Events

        store = ctx.store
        game_id = ctx.game_id

        edge_abort = await self._xcloud_edge_check(ctx)
        if edge_abort is not None:
            return edge_abort

        await self._bus.emit(
            Events.GAME_LAUNCHED,
            store=store,
            game_id=game_id,
            title="",  # No title on LaunchContext
            app_id=0  # No app_id on LaunchContext
        )

        # xCloud streaming URL. ``/play/launch/{productId}`` is the
        # page that *starts the stream* — the old ``/play/games/{id}``
        # was just a store details page (Edge opened but the game never
        # started, which is the "launches Edge, not the game" symptom).
        # Built from the game id directly: the games.map sentinel stores
        # the URL in ``work_dir``, but the dispatcher wraps that field
        # in ``Path`` (which collapses ``https://`` → ``https:/``), so
        # it's not a safe URL source here.
        url = f"https://www.xbox.com/play/launch/{game_id}"

        await emit_stage(
            self._bus,
            i18n_key="toasts.launcher.signingIn",
            game_title=ctx.game_key,
        )

        try:
            # ``EdgeBrowser.launch_xcloud`` is synchronous and
            # returns ``bool``. We dispatch through
            # ``asyncio.to_thread`` because the underlying
            # ``subprocess.Popen[bytes]`` blocks while Edge
            # initializes; without the thread hop the event loop
            # stalls for ~half a second on every launch.
            launched = await asyncio.to_thread(
                self._edge_browser.launch_xcloud, url,
            )
            if not launched:
                return Result(
                    success=False,
                    error="edge_launch_failed",
                    store=store,
                )
            # Block until the streaming session ends — exactly like
            # the native / Windows paths ``await proc.wait()``. Without
            # this the launcher returned immediately, ``GAME_STOPPED``
            # fired at once, and Steam showed the game as stopped while
            # Edge was still streaming (no playtime, stale running
            # indicator, Stop did nothing). Registering the Edge
            # process as the active subprocess also lets SIGTERM-based
            # cancellation (Stop) reach it.
            self._active_subprocess = self._edge_browser.process
            await self._wait_for_xcloud_session()
            return Result(success=True, store=store)
        except Exception as e:
            logger.exception("[LauncherService] xCloud launch failed")
            return Result(success=False, error=str(e))
        finally:
            self._active_subprocess = None
            await self._bus.emit(Events.GAME_STOPPED, store=store, game_id=game_id)

    async def _wait_for_xcloud_session(self) -> None:
        """Block until the Edge streaming session ends.

        Reuses the launcher's canonical xCloud session wait (process
        ``.wait()`` with a max-duration cap + a poll fallback when no
        process handle is available).
        """
        from unifideck.launcher.flows.xcloud import _wait_for_session_end
        await _wait_for_session_end(self._edge_browser)

    async def _get_launch_id_or_none(self) -> str | None:
        """Return the current launch id from ``launch_history`` or None."""
        if self._launch_history:
            return getattr(self._launch_history, "current_launch_id", None)
        return None

    async def _emit_circuit_open_toast(self, ctx: LaunchContext, failure_count: int) -> None:
        """Delegate to ``circuit_breaker.emit_circuit_open_toast``."""
        # The toast helper lives in circuit_breaker.py because the
        # message it renders is specific to the breaker's open state;
        # error_toasts.py only handles post-failure toasts.
        from .circuit_breaker import emit_circuit_open_toast
        await emit_circuit_open_toast(self, ctx, failure_count)

    async def _check_circuit_breaker(self, ctx: LaunchContext) -> bool:
        """Delegate to ``circuit_breaker.check_before_launch``."""
        from .circuit_breaker import check_circuit_breaker
        res = await check_circuit_breaker(self, ctx)
        return res is not None and not res.success

    async def _emit_launcher_error_toast(self, ctx: LaunchContext, err_code: str) -> None:
        """Delegate to ``error_toasts.emit_launcher_error``."""
        from .error_toasts import emit_launcher_error_toast
        await emit_launcher_error_toast(self, ctx, err_code)

    async def _handle_launcher_error(self, ctx: LaunchContext, err: Any) -> Result:
        """Delegate to ``error_toasts.handle_launcher_error``."""
        from .error_toasts import handle_launcher_error
        return await handle_launcher_error(self, ctx, err)

    async def _launch_windows(self, ctx: LaunchContext, state: RuntimeState) -> Result:
        """Delegate to ``orchestrator.launch_windows``."""
        from .orchestrator import launch_windows
        return await launch_windows(self, ctx, state)

    async def _launch_native(self, ctx: LaunchContext, state: RuntimeState) -> Result:
        """Delegate to ``orchestrator.launch_native``."""
        from .orchestrator import launch_native
        return await launch_native(self, ctx, state)

    async def _prepare_windows_plan(
        self,
        ctx: LaunchContext,
        state: RuntimeState,
        *,
        tool_id: str | None = None,
    ) -> ProtonLaunchPlan:
        """Delegate to ``helpers.prepare_windows_plan``."""
        from .helpers import prepare_windows_plan
        return await prepare_windows_plan(self, ctx, state, tool_id=tool_id)

    async def _cloud_sync_phase(self, ctx: LaunchContext, direction: str) -> None:
        """Delegate to ``helpers.cloud_sync_phase``."""
        from .helpers import cloud_sync_phase
        await cloud_sync_phase(self, ctx, direction)

    async def _run_game_subprocess(self, plan: ProtonLaunchPlan, ctx: LaunchContext, state: RuntimeState) -> int:
        """Delegate to ``helpers.run_game_subprocess``."""
        from .helpers import run_game_subprocess
        return await run_game_subprocess(self, plan, ctx, state)

    async def _sync_saves_and_track_size(self, ctx: LaunchContext, phase: str) -> None:
        """Delegate to ``helpers.sync_saves_and_track_size``."""
        from .helpers import sync_saves_and_track_size
        await sync_saves_and_track_size(self, ctx, phase)

    def _resolve_exit_code(self, state: RuntimeState) -> int:
        """Delegate to ``helpers.resolve_exit_code``."""
        from .helpers import resolve_exit_code
        return resolve_exit_code(self, state)

    def _elapsed_since_launch(self) -> float:
        """Delegate to ``helpers.elapsed_since_launch``."""
        from .helpers import elapsed_since_launch
        return elapsed_since_launch(self)
