"""bootstrap.teardown — clean shutdown sequence for the plugin.

Called from the Decky lifecycle hook ``Plugin._unload`` when the
plugin is being deactivated (reload, uninstall, or Steam Deck
shutdown). Ordering matters:

  1. Stop the plugin-owned background loops — they poll on their
     own timers and hold bus subscriptions, so a reload that
     leaves them running strands a task against a dead bus.
  2. Stop every Layer-5 service — they may still be emitting
     events on the bus; letting them run past this point would
     cause writes to dead collaborators.
  3. Stop the PriorityDispatcher — drains the pending queue
     so in-flight events complete before teardown continues.
  4. Clear the EventBus — releases all subscriptions; anything
     that still holds a reference to the bus after this point
     becomes a no-op emitter.

Each step logs its completion so operators debugging a stuck
unload can identify which stage failed. None of the steps
raises — teardown is best-effort; a failure in one stage must
not prevent the later stages from running.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.services.bootstrap import stop_all_services

logger = logging.getLogger(__name__)

#: The plugin-owned background loops, as
#: ``(attribute on Plugin, stop method, label for the log line)``.
#:
#: Each one polls on its own timer and holds a bus subscription, so every
#: one of them outlived a reload before they were stopped here:
#:
#: * ``_updater_service`` — release polling; lightweight, so it goes first.
#: * ``_update_sweep_service`` — can have a store scan in flight.
#: * ``_post_sync_reconcile_service`` — sleeps out a boot delay and may then
#:   have a repair pass running, which would fetch artwork against a
#:   torn-down bus.
#:
#: A table rather than four near-identical blocks: the blocks differed only
#: in these three strings, and the repetition is what pushed
#: ``unload_plugin`` over the cognitive-complexity gate.
_PLUGIN_BACKGROUND_LOOPS: tuple[tuple[str, str, str], ...] = (
    ("_updater_service", "stop_polling", "updater"),
    ("_update_sweep_service", "stop", "update sweep"),
    ("_post_sync_reconcile_service", "stop", "post-sync reconcile"),
)


async def _stop_quietly(target: Any, method: str, label: str) -> None:
    """Await ``target.method()``, logging and swallowing any failure.

    Teardown is best-effort, so one loop refusing to stop must not strand
    the ones queued behind it. A ``None`` target, or one without the stop
    method, is the ordinary case for something that never started and is
    skipped rather than treated as an error.
    """
    if target is None:
        return
    stop = getattr(target, method, None)
    if not callable(stop):
        return
    try:
        await stop()
    except Exception:
        logger.warning("[Unifideck] %s stop failed", label)


async def unload_plugin(plugin: Any) -> None:
    """Execute the full teardown sequence for ``plugin``.

    Args:
        plugin: The ``Plugin`` instance being unloaded. Expected
            attributes: ``services``, ``dispatcher`` (optional),
            ``bus``.

    Never raises — teardown is best-effort. If an exception
    propagates from a service stop, the caller (Decky's lifecycle
    hook) would log it and still proceed; we preserve that
    contract by letting stop_all_services handle its own errors.
    """
    for attr, method, label in _PLUGIN_BACKGROUND_LOOPS:
        await _stop_quietly(getattr(plugin, attr, None), method, label)

    # ``_start_store_background_tasks`` starts the Microsoft token-refresh
    # loop unconditionally at boot, but nothing ever called its stop until
    # now, so every reload left the previous 30-minute poll running against
    # a torn-down bus. It hangs off the registry rather than the plugin, so
    # it cannot join the table above.
    registry = getattr(plugin, "registry", None)
    if registry is not None:
        await _stop_quietly(
            registry.get("microsoft"),
            "stop_token_refresh_polling",
            "Microsoft token poll",
        )

    services = getattr(plugin, "services", None)
    if services is not None:
        await stop_all_services(services)
    if hasattr(plugin, "dispatcher") and plugin.dispatcher is not None:
        await plugin.dispatcher.stop()
        logger.info("[Unifideck] PriorityDispatcher stopped")
    bus = getattr(plugin, "bus", None)
    if bus is not None:
        bus.clear()
    logger.info("[Unifideck] unload complete")
