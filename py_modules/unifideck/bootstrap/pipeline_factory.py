"""bootstrap.pipeline_factory — construct the EventBus pipeline.

Builds the five pipeline primitives and bundles them into a
``BusPipeline`` namedtuple:

  - ``HandlerWatchdog``           — quarantines misbehaving handlers
  - ``HandlerLatencyCollector``   — measures per-handler latency
  - ``EventReplayBuffer``         — retains recent events for replay
  - ``BatchDispatcher``           — batches low-priority events
  - ``PriorityDispatcher``        — queues + routes to handlers

Each primitive is also assigned to the plugin as a ``self.*``
attribute so ``get_bus_health`` (exposed via ``ObservabilityRPCMixin``)
can read their metrics at runtime.

Service instantiation that historically lived in
``_build_eventbus_pipeline`` (``feature_flags``, ``probe_reaction``,
``security``) has been migrated into ``ServiceContainer`` —
those services now go through the same wiring + lifecycle path
as every other Layer-5 service, so this factory does exactly
what its name claims: builds the bus pipeline, nothing more.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.event_bus.priority_dispatcher import PriorityDispatcher

if TYPE_CHECKING:
    from unifideck.event_bus.bus_pipeline import BusPipeline

logger = logging.getLogger(__name__)


async def build_eventbus_pipeline(plugin: Any) -> BusPipeline:
    """Build + start the EventBus pipeline, assigning to ``plugin``.

    Args:
        plugin: The ``Plugin`` instance. Must have ``plugin.bus``
            already initialised. This function mutates plugin by
            setting ``plugin.watchdog``, ``plugin.latency``,
            ``plugin.replay``, ``plugin.batcher``, ``plugin.dispatcher``.

    Returns:
        A ``BusPipeline`` namedtuple containing the same five
        primitives. The dispatcher is already started before
        return — the caller can forward the pipeline to
        ``bootstrap_services`` for any service that needs to
        attach to a specific component (e.g. ``ProbeReactionService``
        consuming ``watchdog`` for handler quarantine reactions).
    """
    from unifideck.event_bus.bus_pipeline import BusPipeline
    from unifideck.event_bus.event_bus_scaling import BatchDispatcher
    from unifideck.event_bus.event_replay import EventReplayBuffer
    from unifideck.event_bus.supervision.metrics_handler import (
        HandlerLatencyCollector,
    )
    from unifideck.event_bus.supervision.watchdog_handler import HandlerWatchdog

    plugin.watchdog = HandlerWatchdog()
    # Attach it to the bus so ``auto_wire`` can find it. Every service calls
    # ``auto_wire(self, bus)`` with two positional args and has no access to
    # this pipeline, so without this the watchdog's ``watchdog=`` parameter
    # was always ``None`` and it tracked zero handlers — for the life of the
    # project (audit register item 4g). Set BEFORE any service is built, or
    # the ones constructed first would silently miss registration.
    plugin.bus.watchdog = plugin.watchdog
    plugin.latency = HandlerLatencyCollector()
    plugin.replay = EventReplayBuffer()
    plugin.batcher = BatchDispatcher()
    # Direct wiring of the replay buffer into the bus so every
    # ``bus.emit`` records to the replay buffer. The
    # ``PriorityDispatcher`` was designed to be the recording
    # path but no caller in the codebase invokes its ``enqueue``
    # method — every emitter goes through ``bus.emit`` directly,
    # so the dispatcher's queue is always empty and the replay
    # buffer never sees anything. Without this hook, the
    # frontend's ``subscribe_replay`` polling always returns an
    # empty list and Sync / Force Sync appear unresponsive even
    # though the backend logs them running.
    plugin.bus.set_replay_recorder(plugin.replay.record)
    plugin.dispatcher = PriorityDispatcher(
        plugin.bus,
        watchdog=plugin.watchdog,
        latency_collector=plugin.latency,
        replay_buffer=plugin.replay,
        batch_dispatcher=plugin.batcher,
    )
    await plugin.dispatcher.start()
    logger.info(
        "[Unifideck] EventBus pipeline ready: dispatcher + "
        "watchdog + metrics + replay + batch",
    )
    return BusPipeline(
        watchdog=plugin.watchdog,
        latency=plugin.latency,
        replay=plugin.replay,
        batcher=plugin.batcher,
        dispatcher=plugin.dispatcher,
    )
