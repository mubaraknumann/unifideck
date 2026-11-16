"""Luna launch orchestration on the 0.7.7 browser-games path (task 7.4).

The live browser-game launch path is
``services/launcher/browser_game.run_browser_game`` (``flows/xcloud.py`` is
dead code and is **not** wired here). ``run_browser_game`` is generic for
xCloud (``kind="stream"``) and itch.io (``kind="web"``); the one Luna-gated
line in it dispatches a ``store == "luna"`` title to
:func:`run_luna_browser_game` here. xCloud and itch.io stay byte-identical.

What this module adds on top of the shared spawn (which xCloud/itch already
do) is the Luna-specific sequence proven mandatory by the feasibility test:

1. **Spawn** the Luna kiosk via :class:`LunaBrowser` — the dedicated Luna
   profile dir, gamepad flags and ``--autoplay-policy`` the generic
   ``edge_browser.launch_browser_game`` does not set — composed over the
   launcher's own ``EdgeBrowser`` so ``svc._active_subprocess`` and
   ``wait_for_browser_exit`` see the spawned process (Stop/SIGTERM reach
   it). The spawn reuses ``_spawn_edge_process`` + ``clean_env()`` (so the
   XAUTHORITY glob / DISPLAY / DBUS injection of 5.11-5.13 is already
   handled); this module only adds the XAUTHORITY-not-found abort (5.13).
2. **Attach** :class:`LunaPlatformSpoofer` browser-level on
   ``browser_game_cdp_port()`` **before** navigation (5.2, 14.1), deriving
   the real browser version (14.8/14.9).
3. **Verify** the spoofed platform equals ``spoof_uach_platform``; a
   mismatch is ``platform_spoofing_failure`` — abort and log the reported
   value (14.12).
4. **Activate** play via :class:`LunaPlayActivator` (detail → play →
   ``/player`` + video playing); failure maps to ``launch_failure`` (5.3-5.5).
5. **Inject** the reused CDP virtual keyboard over the **same** CDP port
   (Req 7); missing CDP aborts the launch and logs it (7.5).
6. **Block** on the shared ``wait_for_browser_exit(..., log_tag="luna")``.

Error mapping to the ``Result`` shape ``run_browser_game`` already returns:

* no supported browser → ``edge_launch_failed`` (non-zero, missing-browser log)
* XAUTHORITY not found → ``xauthority_not_found`` (non-zero, missing-authority log)
* CDP attach failure / undeterminable version / platform mismatch /
  unverifiable probe → ``platform_spoofing_failure``
* missing CDP dependency (keyboard inject / aiohttp) → ``missing_cdp_dependency``
* play-activation failure → ``launch_failure``
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
from unifideck.auth.edge_browser.env import clean_env
from unifideck.core.types import Result
from unifideck.launcher.flows.browser_window import wait_for_browser_exit
from unifideck.stores.luna.luna_browser import LunaBrowser
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_keyboard import inject_virtual_keyboard
from unifideck.stores.luna.luna_platform_spoofer import (
    LunaPlatformSpoofer,
    LunaSpoofConfigError,
)
from unifideck.stores.luna.luna_play_activator import (
    LunaPlayActivationError,
    LunaPlayActivator,
    with_autoplay,
)

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext
    from unifideck.services.launcher.service import LauncherService

logger = logging.getLogger(__name__)

#: Luna's play session ceiling before the launcher stops waiting (4 h),
#: mirroring the shared browser-game ceiling.
_MAX_SESSION_SECONDS = 14400

#: How long to wait for the just-spawned kiosk Chrome to bring up its CDP
#: /json/version endpoint before the spoofer attaches. The spawn and the
#: spoofer attach race otherwise: Chrome's remote-debugging endpoint is not
#: up the instant the process exists, so an immediate version read fails and
#: the spoof never installs ('browser not fully supported').
_CDP_READY_TIMEOUT_S = 10.0


async def _await_cdp_ready(
    cdp: EdgeCDPClient, *, timeout_s: float, interval_s: float = 0.25,
) -> bool:
    """Poll ``cdp.probe_cdp`` until the CDP endpoint answers or time runs out.

    ``probe_cdp`` is a blocking HTTP GET, so it is dispatched via
    :func:`asyncio.to_thread` to keep the event loop free. Returns ``True``
    as soon as the browser answers, ``False`` if the timeout elapses first.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await asyncio.to_thread(cdp.probe_cdp):
            return True
        await asyncio.sleep(interval_s)
    return False

#: Store id and error strings, kept as constants so the launch flow and its
#: tests agree on the exact ``Result.error`` values.
LUNA_STORE = "luna"
ERR_EDGE_LAUNCH_FAILED = "edge_launch_failed"
ERR_XAUTHORITY_NOT_FOUND = "xauthority_not_found"
ERR_PLATFORM_SPOOFING_FAILURE = "platform_spoofing_failure"
ERR_MISSING_CDP_DEPENDENCY = "missing_cdp_dependency"
ERR_LAUNCH_FAILURE = "launch_failure"


def _read_config_int(key: str, default: int) -> int:
    from unifideck.utils.config_helpers import read_config_int_cold_start
    return read_config_int_cold_start(key, default)


def _xauthority_present(env: dict[str, str]) -> bool:
    """Whether a usable ``XAUTHORITY`` file resolved into ``env`` (Req 5.13).

    ``clean_env()`` fills ``XAUTHORITY`` from the gaming-mode session when it
    can; its absence (or a path that is not a file) means the browser would
    fail to reach the X server, so the launch aborts before spawning.
    """
    xauth = env.get("XAUTHORITY")
    return bool(xauth) and Path(xauth or "").is_file()


def _display_available(env: dict[str, str]) -> bool:
    """Whether the kiosk browser can reach any display server (Req 5.13).

    The abort is only justified when there is genuinely no way to reach a
    display: an X11 ``XAUTHORITY`` file *and* a Wayland display are both
    absent. On this Steam Deck in Gaming Mode ``clean_env()`` resolves
    ``XAUTHORITY=None`` (Steam uses a sandbox-internal pressure-vessel
    Xauthority the launcher cannot reach), but ``WAYLAND_DISPLAY=gamescope-0``
    is present and sufficient for the gamescope kiosk — mirroring the working
    xCloud/itch browser-game path, which has no XAUTHORITY gate at all.
    """
    return _xauthority_present(env) or bool(env.get("WAYLAND_DISPLAY"))


async def run_luna_browser_game(svc: LauncherService, ctx: LaunchContext) -> Result:
    """Launch a Luna stream title end-to-end on the browser-games path.

    Called by ``run_browser_game`` for a ``store == "luna"`` title (the one
    luna-gated line there). ``ctx.browser_url`` is the detail URL, already
    allow-list-filtered by ``browser_target`` / ``is_allowed_browser_url``.
    """
    from unifideck.core.types.events import Events

    url = str(ctx.browser_url)
    config = LunaConfig.from_config_manager(svc._config)
    browser = LunaBrowser(config, edge=svc._edge_browser)

    # 5.11-5.13: the shared spawn injects DISPLAY/XDG_RUNTIME_DIR/DBUS/
    # XAUTHORITY/GTK_MODULES via clean_env(); here we only abort when the
    # browser has no way to reach a display at all — neither an X11
    # XAUTHORITY file nor a Wayland display (gamescope-0). With WAYLAND_DISPLAY
    # present we proceed even without XAUTHORITY, like the xCloud/itch path.
    env = clean_env()
    if not _display_available(env):
        logger.warning(
            "[Luna] launch aborted: no X11 XAUTHORITY and no Wayland display resolved"
        )
        return Result(
            success=False, error=ERR_XAUTHORITY_NOT_FOUND, store=ctx.store,
        )

    await svc._bus.emit(
        Events.GAME_LAUNCHED, store=ctx.store, game_id=ctx.game_id,
        title="", app_id=0,
    )
    from unifideck.launcher.rpc import emit_stage
    await emit_stage(svc._bus, i18n_title_key="toasts.header.gameLaunch",
                     i18n_key="toasts.launcher.signingIn",
                     game_title=ctx.game_key)

    try:
        return await _drive_luna_launch(svc, ctx, config, browser, url)
    except Exception:
        logger.exception("[Luna] browser game launch failed")
        return Result(success=False, error=ERR_LAUNCH_FAILURE, store=ctx.store)
    finally:
        svc._active_subprocess = None
        logger.info("[Luna] browser game %s stopped", ctx.game_key)
        await svc._bus.emit(Events.GAME_STOPPED, store=ctx.store, game_id=ctx.game_id)


async def _drive_luna_launch(
    svc: LauncherService,
    ctx: LaunchContext,
    config: LunaConfig,
    browser: LunaBrowser,
    url: str,
) -> Result:
    """Spawn + spoof + verify + activate + keyboard + block (Req 5/7/14)."""
    # Luna starts the title by itself on ``?autoplay=1`` (what its own
    # play deep link redirects to), so the detail page shows only briefly.
    url = with_autoplay(url)
    # ── Spawn (no supported browser → non-zero + missing-browser log) ──
    launched = await asyncio.to_thread(browser.launch, url)
    if not launched:
        logger.warning("[Luna] launch aborted: no supported browser found")
        return Result(success=False, error=ERR_EDGE_LAUNCH_FAILED, store=ctx.store)
    # Register the spawned process so Stop's SIGTERM reaches it and
    # wait_for_browser_exit blocks on the real process (not a no-op).
    svc._active_subprocess = svc._edge_browser.process

    cdp = EdgeCDPClient(cdp_port=svc._edge_browser.browser_game_cdp_port())

    # ── Wait for the kiosk Chrome's CDP endpoint before spoofing ──
    # Chrome's remote-debugging endpoint is not up the instant the process
    # exists; attaching the spoofer immediately races the version read and
    # the spoof never installs. Not fatal on timeout: still proceed into
    # _attach_and_verify, which keeps the existing dead-endpoint handling.
    if not await _await_cdp_ready(cdp, timeout_s=_CDP_READY_TIMEOUT_S):
        logger.warning(
            "[Luna] CDP endpoint not ready after %ss; spoofer attach may fail",
            _CDP_READY_TIMEOUT_S,
        )

    # ── Attach spoofer BEFORE navigation + verify platform ──
    # The spoofer's CDP session must outlive the whole stream: Chrome drops
    # its overrides once that session detaches, so it is closed only after
    # the window is gone.
    spoofer = LunaPlatformSpoofer(config)
    try:
        return await _run_spoofed(svc, ctx, config, cdp, url, spoofer)
    finally:
        await spoofer.close()


async def _run_spoofed(
    svc: LauncherService,
    ctx: LaunchContext,
    config: LunaConfig,
    cdp: EdgeCDPClient,
    url: str,
    spoofer: LunaPlatformSpoofer,
) -> Result:
    """Verify, activate, keyboard and block while ``spoofer`` holds the spoof."""
    spoof_result = await _attach_and_verify(ctx, config, cdp, spoofer)
    if spoof_result is not None:
        return spoof_result

    # ── Play activation (detail → /player + video playing) ──
    play_result = await _activate_play(ctx, config, cdp, url)
    if play_result is not None:
        return play_result

    # ── Virtual keyboard over the SAME CDP port (Req 7; missing CDP → abort) ──
    kbd_result = await _inject_keyboard(svc, ctx, config, cdp)
    if kbd_result is not None:
        return kbd_result

    # ── Block until the window closes (shared helper, log_tag="luna") ──
    await wait_for_browser_exit(
        svc._edge_browser,
        _read_config_int("launcher.browser_game_max_seconds", _MAX_SESSION_SECONDS),
        log_tag="luna",
    )
    logger.info("[Luna] browser game %s ended (window closed)", ctx.game_key)
    return Result(success=True, store=ctx.store)


async def _attach_and_verify(
    ctx: LaunchContext,
    config: LunaConfig,
    cdp: EdgeCDPClient,
    spoofer: LunaPlatformSpoofer,
) -> Result | None:
    """Attach the spoofer before navigation and verify the platform.

    Returns a ``platform_spoofing_failure`` ``Result`` on CDP attach
    failure, undeterminable browser version (:class:`LunaSpoofConfigError`),
    an unreadable verify probe, or a platform mismatch — else ``None`` to
    continue (Req 5.2, 14.1, 14.8/14.9, 14.11/14.12, 14.13).
    """
    try:
        await spoofer.attach(cdp)
    except LunaSpoofConfigError as exc:
        logger.warning(
            "[Luna] platform spoofing failed to attach (reported=%r): %s",
            exc.reported, exc,
        )
        return Result(
            success=False, error=ERR_PLATFORM_SPOOFING_FAILURE, store=ctx.store,
        )

    try:
        reported = await spoofer.verify(cdp)
    except LunaSpoofConfigError as exc:
        logger.warning(
            "[Luna] platform verification unreadable (reported=%r): %s",
            exc.reported, exc,
        )
        return Result(
            success=False, error=ERR_PLATFORM_SPOOFING_FAILURE, store=ctx.store,
        )

    if reported != config.spoof_uach_platform:
        logger.warning(
            "[Luna] platform spoofing mismatch: reported %r, expected %r — abort",
            reported, config.spoof_uach_platform,
        )
        return Result(
            success=False, error=ERR_PLATFORM_SPOOFING_FAILURE, store=ctx.store,
        )
    logger.info("[Luna] platform spoofing verified: %r", reported)
    return None


async def _activate_play(
    ctx: LaunchContext, config: LunaConfig, cdp: EdgeCDPClient, url: str,
) -> Result | None:
    """Run the play-activator; map a failure to ``launch_failure`` (Req 5.3-5.5).

    A structural CDP failure (:class:`LunaPlayActivationError`) and an
    :class:`ActivationResult` failure both map to ``launch_failure`` — the
    title did not start — logged for the launcher. Returns ``None`` on
    success to continue.
    """
    activator = LunaPlayActivator(config)
    player_url = config.player_path
    try:
        result = await activator.activate(cdp, url, player_url)
    except LunaPlayActivationError as exc:
        logger.warning("[Luna] play activation machinery failed: %s", exc)
        return Result(success=False, error=ERR_LAUNCH_FAILURE, store=ctx.store)
    if not result.success:
        logger.warning(
            "[Luna] play activation failed at %s: %s",
            result.step, result.reason,
        )
        return Result(success=False, error=ERR_LAUNCH_FAILURE, store=ctx.store)
    return None


async def _inject_keyboard(
    svc: LauncherService, ctx: LaunchContext, config: LunaConfig, cdp: EdgeCDPClient,
) -> Result | None:
    """Inject the reused virtual keyboard over the same CDP port (Req 7).

    Missing CDP dependency (e.g. ``aiohttp`` unavailable) aborts the launch
    and records the missing-CDP condition, because the spoofer depends on
    CDP too (Req 7.5). Returns ``None`` on success to continue; a failure to
    land on any Luna page is logged but not fatal (the stream still plays).
    """
    locale = svc._edge_browser.locale_fn()
    try:
        injected = await inject_virtual_keyboard(
            cdp.cdp_port, locale, url_patterns=[config.luna_host or "luna.amazon"],
        )
    except ImportError as exc:
        logger.warning("[Luna] missing CDP dependency for keyboard inject: %s", exc)
        return Result(
            success=False, error=ERR_MISSING_CDP_DEPENDENCY, store=ctx.store,
        )
    if not injected:
        logger.info(
            "[Luna] virtual keyboard not injected (no matching Luna page); "
            "continuing without it",
        )
    return None
