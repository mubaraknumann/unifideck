"""bootstrap.teardown — clean shutdown sequence for the plugin.

Called from the Decky lifecycle hook ``Plugin._unload`` when the
plugin is being deactivated (reload, uninstall, or Steam Deck
shutdown). Ordering matters:

  1. Stop every Layer-5 service — they may still be emitting
     events on the bus; letting them run past this point would
     cause writes to dead collaborators.
  2. Stop the PriorityDispatcher — drains the pending queue
     so in-flight events complete before teardown continues.
  3. Clear the EventBus — releases all subscriptions; anything
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
    # Stop updater background polling first — lightweight, fast.
    updater = getattr(plugin, "_updater_service", None)
    if updater is not None:
        try:
            await updater.stop_polling()
        except Exception:
            logger.warning("[Unifideck] updater stop_polling failed")
    # Same for the game-update sweep: it holds a bus subscription and can
    # have a store scan in flight, both of which must not outlive a reload.
    sweep = getattr(plugin, "_update_sweep_service", None)
    if sweep is not None:
        try:
            await sweep.stop()
        except Exception:
            logger.warning("[Unifideck] update sweep stop failed")
    # The post-sync reconcile sleeps out a boot delay and may then have a
    # repair pass in flight; both must be cancelled here, or a reload
    # leaves a task fetching artwork against a torn-down bus.
    reconcile = getattr(plugin, "_post_sync_reconcile_service", None)
    if reconcile is not None:
        try:
            await reconcile.stop()
        except Exception:
            logger.warning("[Unifideck] post-sync reconcile stop failed")
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
