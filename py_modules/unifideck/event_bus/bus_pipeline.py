"""Bus pipeline orchestrator — composes the EventBus + extensions.

``BusPipeline`` is a small dataclass-like container that holds the
fully-composed bus stack — the core ``EventBus`` plus all the
extensions (replay buffer, circuit breaker, batch dispatcher,
metrics handler, watchdog).

Constructed by ``services/bootstrap/constructor.py`` at boot time
through ``Plugin._build_eventbus_pipeline``. Components that need
to subscribe at boot time (cache invalidator, persistence services)
receive the pipeline reference and pull the components they care
about from it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .event_bus_scaling import BatchDispatcher
    from .event_replay import EventReplayBuffer
    from .priority_dispatcher import PriorityDispatcher
    from .supervision.metrics_handler import HandlerLatencyCollector
    from .supervision.watchdog_handler import HandlerWatchdog

class BusPipeline(NamedTuple):
    """Bus pipeline."""

    watchdog: HandlerWatchdog
    latency: HandlerLatencyCollector
    replay: EventReplayBuffer
    batcher: BatchDispatcher
    dispatcher: PriorityDispatcher
