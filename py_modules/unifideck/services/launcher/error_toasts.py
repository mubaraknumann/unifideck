"""services/launcher/error_toasts.py — Post-failure user reporting.

3 functions handling the aftermath of a ``LauncherError`` raised
during launch. ``emit_launch_error_toast`` is the shared delivery
used by every terminal-failure toast in this package;
``emit_launcher_error_toast`` renders the catch-all one;
``handle_launcher_error`` classifies the error (record in circuit
breaker unless it's a user cancel) and fires the toast.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Result

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext

    from .service import LauncherService

logger = logging.getLogger(__name__)

#: Terminal failures hold the toast open far longer than a progress
#: stage. Long enough to read on a handheld, short enough to dismiss
#: itself if the user has already walked away.
_ERROR_TOAST_MS = 10000


async def emit_launch_error_toast(
    svc: LauncherService,
    ctx: LaunchContext,
    *,
    i18n_key: str,
    extra_params: dict[str, Any] | None = None,
    tag: str,
) -> None:
    """Deliver one terminal-failure toast for the current launch.

    **``LAUNCHER_STAGE`` is the only channel that reaches the UI from
    here.** This code runs in the launcher *subprocess* — ``LauncherService``
    is built solely by ``launcher.bootstrap`` — and that process's bus dies
    with it. ``frontend_bridge.install_bus_forwarder`` mirrors
    ``LAUNCHER_STAGE``, and nothing else, into the file the plugin drains.
    Until 2026-08 both callers emitted ``TOAST_NOTIFICATION``, which had no
    forwarder and no subscriber in either process: a circuit-breaker refusal
    and a terminal launch failure were both completely silent, so a game
    that had failed three times simply stopped responding to Play with no
    message at all. Anything added here must stay on ``LAUNCHER_STAGE``.

    Every string these callers use declares a ``{{game_key}}`` placeholder,
    so the resolved display title is fed into it: that turns "battlenet:D1
    failed to launch" into "Diablo IV failed to launch" without touching 16
    locale files. ``resolve_title`` falls back to the raw key.

    Best-effort by construction — a failure to *report* a failed launch must
    not itself raise into the launch path.

    Args:
        i18n_key: the locale key to render.
        extra_params: interpolation values beyond ``game_key``.
        tag: log prefix, so a delivery failure names its caller.
    """
    from unifideck.launcher.game_title import resolve_title
    from unifideck.launcher.rpc import emit_stage

    title = resolve_title(ctx.game_key)
    try:
        await emit_stage(
            svc._bus,
            i18n_key=i18n_key,
            game_title=title,
            severity="error",
            duration_ms=_ERROR_TOAST_MS,
            i18n_params={"game_key": title, **(extra_params or {})},
        )
    except Exception as e:
        logger.warning("[%s] Failed to emit error toast: %s", tag, e)


async def emit_launcher_error_toast(
    svc: LauncherService,
    ctx: LaunchContext,
    err_code: str,
) -> None:
    """Emit a user-facing error toast for a LauncherError.

    The catch-all for a launch that died on an error nothing more specific
    handled — the specific failures (umu retry, prefix init, GE fallback)
    toast from their own sites.
    """
    await emit_launch_error_toast(
        svc, ctx,
        i18n_key="toasts.launcher.launcherError",
        extra_params={"error_code": err_code},
        tag="ErrorToasts",
    )


async def handle_launcher_error(
    svc: LauncherService,
    ctx: LaunchContext,
    err: Exception,
) -> Result:
    """Convert a LauncherError into a failure Result."""
    err_code = getattr(err, "code", type(err).__name__)
    err_msg = str(err)

    is_cancel = "cancel" in err_code.lower() or "cancel" in err_msg.lower()

    if not is_cancel and svc._launch_history:
        try:
            # Record failure via FAILURE_KIND_LAUNCHER_ERROR
            store = ctx.store
            game_id = ctx.game_id
            game_key = f"{store}:{game_id}"

            svc._launch_history.record_failure(
                game_key,
                "launcher_error",
                err_code
            )
        except Exception as e:
            logger.debug("[ErrorToasts] Failed to record failure: %s", e)

    await emit_launcher_error_toast(svc, ctx, err_code)

    # ``Result`` has no ``message`` field — its public surface is
    # ``success``, ``error``, ``error_code``, ``store``, and
    # ``metadata``. The human-readable text belongs in ``metadata``
    # so the toast helper can pick it up while the canonical
    # ``error`` slot holds the machine code. An earlier version
    # passed ``message=err_msg`` and raised
    # ``TypeError: Result.__init__() got an unexpected keyword
    # argument 'message'`` on every classified launch failure.
    # ``error_code`` mirrors ``error`` here: the dispatcher's exit-code map
    # reads only ``error_code``, so without it every classified launcher
    # error exited GAME_FAILED (8) and the classification computed just above
    # was thrown away. Audit register item 4c.
    return Result(
        success=False,
        error=err_code,
        error_code=err_code,
        metadata={"message": err_msg},
    )
