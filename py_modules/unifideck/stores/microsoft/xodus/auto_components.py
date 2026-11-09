"""stores/microsoft/xodus/auto_components.py — keep the components current.

With Xbox PC downloads on and Microsoft connected, the components (our
Xodus build and runtime) install, or update to the newer build in the source
folder, without anyone pressing a button.

This runs in the backend, when the Microsoft sign-in is first seen as valid.
The frontend's boot step could not do it: the Microsoft availability is
cached as "signed out" until the first token check, and that check usually
lands after the frontend has looked (ISSUES.md I-029).

A failed install is not retried here, so a broken source folder does not
reinstall on every sign-in check. The failure shows in the status and as a
toast, and the next toggle or sign-in tries again.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .xbox_pc import XboxPc

logger = logging.getLogger(__name__)

#: Runtime states that need the install to run.
_NEEDS_INSTALL = frozenset({"missing", "outdated"})

_tasks: set[asyncio.Task[None]] = set()


async def ensure_components(facade: XboxPc) -> None:
    """Install or update the components when the toggle is on and they need it."""
    if not facade.enabled():
        return
    status = await facade.refresh_components()
    state = status["runtime"]["state"]
    if state not in _NEEDS_INSTALL or status["runtime"].get("error"):
        return
    result = await facade.install_components()
    logger.info(
        "[XboxPc] components %s: %s", state,
        "installing in the background" if result["started"] else result["reason"],
    )


def schedule(facade: XboxPc) -> None:
    """Run :func:`ensure_components` in the background. No-op without a loop."""
    try:
        task = asyncio.get_running_loop().create_task(ensure_components(facade))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
