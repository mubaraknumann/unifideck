"""services/launcher/circuit_breaker.py — Pre-launch failure protection.

2 functions protecting a launch from being attempted when the
game has repeatedly failed recently. Circuit breaker state
lives in ``LaunchHistoryService``; this module consults it and
surfaces the refusal to the user.

A third, ``get_launch_id_or_none``, was deleted in 2026-08: its only
purpose was to build a "Show logs" toast action pointing at
``unifideck://show-logs/<launch_id>``, and no frontend renders a toast
action button — both toast renderers special-case the cloud-save
``retry-sync`` modal and drop everything else, and the ``LaunchLogsModal``
that verb targets was never built. See the audit register.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Result

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext

    from .service import LauncherService

logger = logging.getLogger(__name__)


async def emit_circuit_open_toast(
    svc: LauncherService,
    ctx: LaunchContext,
    failure_count: int,
) -> None:
    """Emit an error toast when the circuit breaker refuses launch.

    Delivery, and the reasons it must stay on ``LAUNCHER_STAGE``, live in
    :func:`~.error_toasts.emit_launch_error_toast`.
    """
    from .error_toasts import emit_launch_error_toast

    await emit_launch_error_toast(
        svc, ctx,
        i18n_key="toasts.launcher.errorCircuitBreakerOpen",
        extra_params={"count": failure_count},
        tag="CircuitBreaker",
    )


async def check_circuit_breaker(
    svc: LauncherService,
    ctx: LaunchContext,
) -> Result | None:
    """Return a refusal Result if the breaker is open."""
    if not svc._launch_history:
        return None

    store = ctx.store
    game_id = ctx.game_id
    game_key = f"{store}:{game_id}"

    try:
        # Assuming LaunchHistoryService has a method to check if circuit is open
        is_open, failure_count = svc._launch_history.is_circuit_open(game_key)

        if is_open:
            logger.warning("[CircuitBreaker] Circuit open for %s (failures: %d)", game_key, failure_count)
            await emit_circuit_open_toast(svc, ctx, failure_count)
            # ``Result`` has no ``message`` field — its public surface
            # is ``success``, ``error``, ``error_code``, ``store``,
            # ``metadata``. Same fix as ``error_toasts.py``: route
            # the human-readable text through ``metadata`` so the
            # toast helper can pick it up while the canonical
            # ``error`` slot holds the machine code. The earlier
            # ``message=`` form raised
            # ``TypeError: Result.__init__() got an unexpected
            # keyword argument 'message'`` every time the circuit
            # breaker engaged, swallowing the actual "circuit open"
            # signal under a TypeError noise.
            return Result(
                success=False,
                error="circuit_open",
                # See ``LauncherService._circuit_open_result``: the exit-code
                # map reads ``error_code``, not ``error`` (item 4c).
                error_code="circuit_open",
                metadata={
                    "message": (
                        f"Launch refused. Game failed "
                        f"{failure_count} times recently."
                    ),
                },
            )

    except Exception as e:
        logger.debug("[CircuitBreaker] Failed to check circuit state: %s", e)

    return None
