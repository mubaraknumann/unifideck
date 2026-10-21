"""stores/microsoft/xodus/single_signin.py — Connect Microsoft = the Xbox PC sign-in.

With Xbox PC downloads on there is one Microsoft sign-in, and it is the
Xodus one: xCloud, Game Pass and ownership run on the ticket it provides
(``ticket.py``). ``MicrosoftStore.start_auth`` comes here instead of the
OAuth flows. The frontend treats it like any store sign-in: it launches the
Microsoft auth shortcut (the same Edge window the Xodus sign-in drives) and
waits for ``STORE_AUTH_COMPLETE`` or ``STORE_AUTH_FAILED``, which this module
emits when the Xodus sign-in ends.

The components must be installed before the sign-in can run. Turning the
toggle on installs them; a Connect that comes first waits for them.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from unifideck.core.types import AuthResult, Events

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

    from .xbox_pc import XboxPc

logger = logging.getLogger(__name__)

#: How long a Connect waits for the components (copy and unpack, 1 to 2 minutes).
COMPONENTS_WAIT_S = 600.0
_SIGNED_IN = frozenset({"signed_in", "unverified"})
_USABLE = frozenset({"installed", "outdated"})
_tasks: set[asyncio.Task[None]] = set()


async def _components_ready(facade: XboxPc) -> str | None:
    """Wait for usable components; None, or why they are not there."""
    status = await facade.refresh_components()
    if status["runtime"]["state"] not in _USABLE and status["runtime"]["state"] != "installing":
        started = await facade.install_components()
        if not started["started"] and started["reason"] != "install_in_progress":
            return str(started["reason"])
    deadline = time.monotonic() + COMPONENTS_WAIT_S
    while time.monotonic() < deadline:
        runtime = (await facade.status())["runtime"]
        if runtime["state"] in _USABLE:
            return None
        if runtime["state"] != "installing":
            return str(runtime.get("error") or "xbox_pc_runtime_missing")
        await asyncio.sleep(1.0)
    return "xbox_pc_runtime_missing"


def _outcome_listener(bus: EventBus, facade: XboxPc) -> Callable[[dict[str, Any]], None]:
    """A one-shot sign-in listener that reports the end as a store sign-in."""
    def done(account: dict[str, Any]) -> None:
        with contextlib.suppress(ValueError):
            facade.sign_in_listeners.remove(done)
        state = str(account.get("state"))
        if state in _SIGNED_IN:
            event, extra = Events.STORE_AUTH_COMPLETE, {}
        else:
            event, extra = Events.STORE_AUTH_FAILED, {"error": str(account.get("error") or "window_closed")}
        logger.info("[XboxPc] Microsoft sign-in (one sign-in) ended: %s", state)
        task = asyncio.get_running_loop().create_task(bus.emit(event, store="microsoft", **extra))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return done


async def start_connect(facade: XboxPc, bus: EventBus) -> AuthResult:
    """Start the one Microsoft sign-in. Returns at once while the window is pending."""
    reason = await _components_ready(facade)
    if reason is not None:
        return AuthResult(success=False, error=reason, store="microsoft")
    listener = _outcome_listener(bus, facade)
    facade.sign_in_listeners.append(listener)
    result = await facade.start_sign_in(connect=True)
    if not result.get("pending"):
        with contextlib.suppress(ValueError):
            facade.sign_in_listeners.remove(listener)
        return AuthResult(success=False, error=str(result.get("reason") or "unknown"), store="microsoft")
    return AuthResult(
        success=True, store="microsoft", url=None,
        metadata={"pending": True, "flow": "xbox_pc"},
    )
