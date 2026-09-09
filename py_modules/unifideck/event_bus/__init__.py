"""Event bus sub-package — public surface.

Re-exports the canonical event-bus symbols a caller (services, RPC
mixins, stores) typically reaches for : ``EventBus`` itself, the
``EventPriority`` enum, the ``BusPipeline`` orchestrator, and the
typed extension classes (``EventPayload``, ``EventSchema``,
``TypedEventRegistry``, ``DeadLetterQueue``, ``DebugSnapshot``).

The internal modules (``priority_dispatcher``, ``event_replay``,
``event_bus_reliability``, ``event_bus_scaling``, ``event_bus_devex``)
are not re-exported — they're glued together by ``BusPipeline`` and
the ``EventBus`` constructor, not consumed directly.

Architecture role : the message backbone, at the layer ``event_bus/``
occupies in the diagram in ``docs/architecture.md`` (authoritative; do
not restate a layer count here). Sits below the service layer (which
emits and consumes events) and above the cache + config layers (which
the bus relies on for state).
"""

from .event_bus import EventBus
from .event_bus_devex import (
    SchemaExtractor,
    auto_wire,
    subscribe,
)
from .event_bus_extensions import (
    DeadLetterQueue,
    DebugSnapshot,
    EventSchema,
    PredicateFilter,
    TypedEventRegistry,
)
from .event_bus_reliability import CircuitBreaker
from .event_bus_scaling import BatchDispatcher
from .event_priority import (
    EventPriority,
    get_coalesce_key,
    get_priority,
)
from .event_replay import EventReplayBuffer
from .priority_dispatcher import PriorityDispatcher
from .supervision.metrics_handler import (
    HandlerLatencyCollector,
    HandlerLatencyStats,
)
from .supervision.watchdog_handler import (
    HandlerQuarantinedError,
    HandlerWatchdog,
)

__all__ = [
    "BatchDispatcher",
    "CircuitBreaker",
    "DeadLetterQueue",
    "DebugSnapshot",
    "EventBus",
    "EventPriority",
    "EventReplayBuffer",
    "EventSchema",
    "HandlerLatencyCollector",
    "HandlerLatencyStats",
    "HandlerQuarantinedError",
    "HandlerWatchdog",
    "PredicateFilter",
    "PriorityDispatcher",
    "SchemaExtractor",
    "TypedEventRegistry",
    "auto_wire",
    "get_coalesce_key",
    "get_priority",
    "subscribe",
]
