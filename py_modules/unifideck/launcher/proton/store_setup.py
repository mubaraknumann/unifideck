"""Per-store steps that finish a prefix after ``setup_prefix``.

A table, like ``_STORE_LAUNCHERS``, so the install warmup needs no store-name
branch: a store whose games need more than the generic prefix setup adds a row.
EA's builds must run their own installer script (Touchup) in the prefix before
the game can start (``launcher/ea/touchup.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from unifideck.launcher.ea.touchup import prepare_prefix as ea_prepare_prefix
from unifideck.launcher.types.context import LaunchContext, RuntimeState

logger = logging.getLogger(__name__)

#: ``(ctx, state, session_env)``. ``session_env`` is the user-session env the
#: headless install warmup borrows (display, D-Bus); None at launch, where the
#: launcher already runs in the session.
PrefixStep = Callable[[LaunchContext, RuntimeState, "dict[str, str] | None"], Awaitable[bool]]

STORE_PREFIX_SETUP: dict[str, PrefixStep] = {
    "ea": ea_prepare_prefix,
}


async def run_store_prefix_setup(
    ctx: LaunchContext, state: RuntimeState, session_env: dict[str, str] | None = None,
) -> bool:
    """Run the store's extra prefix step, if it has one; True when none was needed."""
    step = STORE_PREFIX_SETUP.get(ctx.store)
    if step is None:
        return True
    return await step(ctx, state, session_env)
