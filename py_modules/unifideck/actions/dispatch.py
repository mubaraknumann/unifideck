from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from unifideck.actions.unifideck_uri import SCOPE_FRONTEND, parse_unifideck_uri
from unifideck.rpc import RpcError

if TYPE_CHECKING:
    from unifideck.core.sync_service import SyncService
    from unifideck.services.cloud_save import CloudSaveService
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)

# Fire-and-forget tasks scheduled by dispatch handlers (refresh-library,
# refresh-all). We keep strong references in a module-level set so the
# tasks aren't garbage-collected mid-flight — without this, the Python
# event loop only holds a weak reference and the task can vanish.
# Each task removes itself on completion via the discard callback.
_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_background(coro: Any, *, name: str) -> None:
    """Schedule ``coro`` as a tracked background task."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

async def dispatch_backend_action(*, uri: str, registry: StoreRegistry,
    cloudsave: CloudSaveService | None, sync_service: SyncService | None) -> Any:
    """Dispatch backend action."""
    action = parse_unifideck_uri(uri)
    if not action.valid:
        raise RpcError("invalid_uri", reason=action.error, uri=uri)
    if action.scope == SCOPE_FRONTEND:
        raise RpcError(
            "frontend_scope_verb",
            verb=action.verb,
            hint="frontend should handle settings/* locally",
        )
    if action.verb == "auth":
        return await _dispatch_auth(action, registry)
    if action.verb == "retry-sync":
        return await _dispatch_retry_sync(action, cloudsave)
    if action.verb == "refresh-library":
        return _dispatch_refresh_library(action, sync_service)
    if action.verb == "refresh-all-libraries":
        return _dispatch_refresh_all(sync_service)
    raise RpcError(
        "unhandled_backend_verb",
        verb=action.verb,
        hint="add a handler in dispatch_backend_action",
    )

async def _dispatch_auth(action: Any, registry: StoreRegistry) -> Any:
    """Dispatch auth."""
    store = action.args[0]
    return await registry.auth_action(store, "start")

async def _dispatch_retry_sync(action: Any,
    cloudsave: CloudSaveService | None) -> dict[str, Any]:

    """Dispatch retry sync."""
    if cloudsave is None:
        raise RpcError("service_unavailable", service="cloudsave")
    store, game_id, phase = action.args
    if phase == "sync_down":
        # This phase is only reached via the explicit "Use Cloud" conflict
        # choice, so force the pull — otherwise the store tool skips the
        # download whenever the local save is newer/same-age and "Use Cloud"
        # would do nothing.
        result = await cloudsave.sync_down(store, game_id, force=True)
    elif phase == "sync_up":
        result = await cloudsave.sync_up(store, game_id)
    else:
        raise RpcError(
            "invalid_phase",
            phase=phase,
            supported=["sync_down", "sync_up"],
        )
    return {
        "success": result.success,
        "error": result.error,
        "store": store,
        "game_id": game_id,
        "phase": phase,
    }

def _dispatch_refresh_library(action: Any,
    sync_service: SyncService | None) -> dict[str, Any]:
    """Dispatch refresh library."""
    if sync_service is None:
        raise RpcError("service_unavailable", service="sync_service")
    store = action.args[0]
    # A one-store run through the normal queue: it waits for an in-flight
    # sync instead of racing it, and runs the post-sync phases for that
    # store's games.
    _spawn_background(
        sync_service.sync_all(stores=[store], source=f"refresh:{store}"),
        name=f"refresh-library-{store}",
    )

    return {"success": True, "store": store, "status": "scheduled"}

def _dispatch_refresh_all(sync_service: SyncService | None) -> dict[str, Any]:
    """Dispatch refresh all."""
    if sync_service is None:
        raise RpcError("service_unavailable", service="sync_service")

    _spawn_background(
        sync_service.sync_all(),
        name="refresh-all-libraries",
    )

    return {"success": True, "status": "scheduled"}
