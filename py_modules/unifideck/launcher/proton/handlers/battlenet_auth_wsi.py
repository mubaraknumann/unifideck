"""Running the sign-in client, with the WSI measurement the auth path lacked.

py_modules/unifideck/launcher/proton/handlers/battlenet_auth_wsi.py

``_start_client_here`` has always measured the gamescope-WSI abort: start,
fail, read the game log, record the host marker, retry with the layer off.
``battlenet_auth_launch`` never did, and that closed a loop no user could
escape from inside the product (GitHub #446, reproduced on a ROG Xbox Ally X)::

    first sign-in -> no marker -> ANGLE aborts in the WSI layer
                  -> nothing reads the log -> marker never written
                  -> no window, ever, on every later attempt

The reporter broke the loop by hand-writing
``~/.local/share/unifideck/battlenet_gamescope_wsi.json``, which is the
strongest possible confirmation and also not something to ask a user to do.

**Why this cannot live inside ``run_umu_with_retry``.** That function decides
on exit code and duration, and it returns on ``rc == 0`` before any retry
logic runs. The abort this recovers from *is* an rc 0 — the Wine session dies
under umu and umu reports success (see :mod:`battlenet_wsi`). Even on a
non-zero rc its retry would relaunch with the same environment and no marker,
straight back into the same crash. The measurement therefore has to happen
after the whole call returns, which is what this module is.

The evidence that separates the two identical-looking exits is
:class:`battlenet_watch.ReadinessLatch`: a renderer that never came up was a
crash, a renderer that came up and went away was a user closing the window.
That is the same latch :func:`auth_retry_worthwhile` already uses, read here
for the opposite decision.

Takes ``teardown`` and ``clear_stale`` as callables rather than importing
them: they live in ``battlenet.py``, which imports this module, and naming
them here would close the loop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.handlers import battlenet_wsi
from unifideck.launcher.proton.handlers.battlenet_auth_retry import (
    auth_retry_worthwhile,
)
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry

logger = logging.getLogger(__name__)

#: The sign-in shortcut is not a game — ``resolve_title`` has no registry row
#: for ``battlenet:bnet-auth`` and returns the key itself. The retry toast
#: carries no ``{{gameTitle}}`` placeholder, so an empty title is correct
#: here rather than a key leaking into the user's screen.
_NO_TITLE = ""

Teardown = Callable[[ProtonLaunchPlan], AbstractAsyncContextManager[None]]
ClearStale = Callable[[ProtonLaunchPlan], Awaitable[None]]


async def _one_attempt(
    plan: ProtonLaunchPlan, argv: list[str], *, teardown: Teardown,
) -> tuple[int, bool]:
    """Run the sign-in client once. Returns ``(rc, renderer_was_seen)``.

    ``reap_wineserver=False`` so a stop from the UI unwinds through the
    teardown's SIGTERM instead of SIGKILLing the client outright: the token
    the client rotated during sign-in lives in ``CachedData.db`` and is lost
    if it never gets to flush. See ``watch.stop_client``.
    """
    async with teardown(plan), watch.watch_readiness(plan.prefix_path) as ready:
        rc = await run_umu_with_retry(
            argv, env=plan.env, on_start=plan.on_process_start,
            reap_wineserver=False,
            should_retry=lambda: auth_retry_worthwhile(plan, ready),
        )
    return rc, ready.seen


async def run_auth_client(
    plan: ProtonLaunchPlan,
    argv: list[str],
    *,
    teardown: Teardown,
    clear_stale: ClearStale,
) -> int:
    """Run the sign-in client, retrying once if the WSI layer provably killed it.

    The second run is only ever reached when all three hold: the renderer was
    never seen, the layer was on, and the client's own log carries the
    ANGLE-in-WSI signature. Anything else — including a user closing a window
    that did appear — returns the first attempt's code untouched.
    """
    rc, seen = await _one_attempt(plan, argv, teardown=teardown)
    if seen:
        return rc
    logger.info(
        "[battlenet] the sign-in client exited (rc=%d) without a renderer ever "
        "appearing; checking whether it aborted in the gamescope WSI layer", rc,
    )
    if not await battlenet_wsi.adopt_workaround(plan, _NO_TITLE):
        return rc
    await clear_stale(plan)
    rc, _ = await _one_attempt(plan, argv, teardown=teardown)
    return rc
