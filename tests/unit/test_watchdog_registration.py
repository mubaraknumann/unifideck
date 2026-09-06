"""The handler watchdog must actually see the handlers — audit item 4g.

``HandlerWatchdog`` was constructed at boot, handed to ``PriorityDispatcher``,
exposed to the observability mixin, torn down on unload — and tracked **zero
handlers for the life of the project**. Its ``register()`` is reached only
from ``auto_wire``'s ``watchdog=`` parameter, and every one of the ~20
``auto_wire`` call sites passes two positional arguments, so the parameter
was always ``None``.

Nothing surfaced it because the failure looks like health: a Capture Logs
bundle taken 2026-08-25 reported ``frontend.bus_health.watchdog = {}`` while
the same bundle showed 42 registered events and a live security audit trail.
An empty block reads as "nothing wrong" rather than "not wired" — the same
shape as the DiagnosticsPanel cluster in §1.2.

**The fix is deliberately not "pass the watchdog at 20 call sites".** Services
are constructed with a bus and have no access to the bus pipeline, so
threading it by hand would mean a new constructor parameter on every service —
a wide change with 20 chances to miss one, and nothing to catch the miss.
``pipeline_factory`` attaches the watchdog to the **bus** instead, and
``auto_wire`` falls back to it. One assignment, no call-site churn, and a
service that forgets nothing because it never had to remember.

The ordering matters and is asserted below: the pipeline is built in
``_boot_layer2_core`` and services in ``_boot_layer5_services``, so every
service is constructed after the attachment. A service built before it would
silently miss registration — which is exactly the bug, one layer up.
"""
from __future__ import annotations

from typing import Any

from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus import EventBus
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.event_bus.supervision.watchdog_handler import HandlerWatchdog


class _Service:
    """Wired exactly the way every real service wires itself."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        auto_wire(self, bus)

    @subscribe(Events.GAME_STOPPED)
    async def _on_game_stopped(self, **kwargs: Any) -> None: ...

    @subscribe(Events.GAME_LAUNCHED)
    async def _on_game_launched(self, **kwargs: Any) -> None: ...


def _tracked(watchdog: HandlerWatchdog) -> set[str]:
    return set(getattr(watchdog, "_metrics", {}))


def test_a_bus_carrying_a_watchdog_registers_every_handler() -> None:
    """The regression. Fails against the pre-fix ``auto_wire``."""
    bus = EventBus()
    watchdog = HandlerWatchdog()
    bus.watchdog = watchdog  # what pipeline_factory now does

    _Service(bus)

    assert _tracked(watchdog) == {
        "_Service._on_game_stopped",
        "_Service._on_game_launched",
    }


def test_a_bus_without_a_watchdog_still_wires_its_handlers() -> None:
    """The launcher subprocess has no pipeline and must not break.

    ``build_service_subset`` constructs a reduced service graph on its own
    bus with no ``HandlerWatchdog`` at all. Subscription must still work
    there — the watchdog is observability, not a dependency.
    """
    bus = EventBus()
    service = _Service(bus)

    assert not hasattr(bus, "watchdog") or bus.watchdog is None
    # Handlers are on the bus regardless.
    assert bus._handlers.get(Events.GAME_STOPPED.value)
    assert service is not None


def test_an_explicit_watchdog_argument_still_wins() -> None:
    """The parameter keeps working; the bus is only a fallback."""
    bus = EventBus()
    on_bus = HandlerWatchdog()
    explicit = HandlerWatchdog()
    bus.watchdog = on_bus

    service = _Service.__new__(_Service)
    service._bus = bus
    auto_wire(service, bus, watchdog=explicit)

    assert _tracked(explicit)
    assert _tracked(on_bus) == set()


def test_the_pipeline_attaches_the_watchdog_before_services_are_built() -> None:
    """Ordering is the whole correctness argument.

    ``boot_plugin`` runs ``_boot_layer2_core`` (which builds the pipeline)
    before ``_boot_layer5_services`` (which constructs every service). If
    that ever inverted, services would be wired against a bus with no
    watchdog and this fix would silently stop working — the same
    reads-as-healthy failure it exists to close.
    """
    import inspect

    from unifideck.bootstrap import boot

    source = inspect.getsource(boot)
    core = source.index("_boot_layer2_core(plugin")
    services = source.index("_boot_layer5_services(plugin")
    assert core < services, (
        "layer 2 (pipeline) must be booted before layer 5 (services)"
    )


def test_pipeline_factory_attaches_it_to_the_bus() -> None:
    """Pins the one assignment the whole mechanism depends on."""
    import inspect

    from unifideck.bootstrap import pipeline_factory

    source = inspect.getsource(pipeline_factory)
    assert "plugin.bus.watchdog = plugin.watchdog" in source, (
        "pipeline_factory must attach the watchdog to the bus, or auto_wire "
        "has nothing to fall back to and the watchdog tracks nothing again"
    )


# ── G1b: the bus actually invokes through the watchdog ──────────
async def test_the_bus_invokes_handlers_through_the_watchdog() -> None:
    """``HandlerWatchdog.invoke`` had zero callers until 2026-08-26.

    It holds the per-handler timeout and the quarantine escalation, and
    ``priority_dispatcher``'s own docstring claimed "the bus uses the
    watchdog internally". It did not. The bus enforced only its own flat
    60s ``asyncio.wait_for``, so repeated timeouts never escalated and the
    metrics stayed empty.
    """
    bus = EventBus()
    watchdog = HandlerWatchdog()
    bus.watchdog = watchdog
    seen: list[str] = []

    async def handler(**kwargs: Any) -> str:
        seen.append("async")
        return "ok"

    bus.on("t_ev", handler)
    await bus.emit("t_ev", a=1)

    assert seen == ["async"]
    metrics = getattr(watchdog, "_metrics", {})
    assert any(m.invocations == 1 for m in metrics.values()), (
        "the watchdog must have counted the invocation"
    )


async def test_a_sync_handler_still_runs_off_the_event_loop() -> None:
    """``watchdog.invoke`` awaits the handler directly.

    A plain function would return a non-awaitable and raise, so the bus
    passes a thunk returning ``asyncio.to_thread(...)``. Without that,
    every sync subscriber would have broken the moment supervision landed.
    """
    bus = EventBus()
    bus.watchdog = HandlerWatchdog()
    seen: list[str] = []

    def sync_handler(**kwargs: Any) -> str:
        seen.append("sync")
        return "ok"

    bus.on("t_sync", sync_handler)
    await bus.emit("t_sync", a=1)

    assert seen == ["sync"]


async def test_a_quarantined_handler_is_skipped_and_the_others_still_run() -> None:
    """Quarantine must degrade one handler, not the event."""
    bus = EventBus()
    watchdog = HandlerWatchdog()
    bus.watchdog = watchdog
    seen: list[str] = []

    async def bad(**kwargs: Any) -> None:
        seen.append("bad")

    async def good(**kwargs: Any) -> None:
        seen.append("good")

    bus.on("t_q", bad)
    bus.on("t_q", good)
    await bus.emit("t_q", a=1)  # registers both in the metrics
    seen.clear()

    name = next(n for n in getattr(watchdog, "_metrics", {}) if "bad" in n)
    watchdog.quarantine_preemptive(name, reason="test")
    results = await bus.emit("t_q", a=2)

    assert seen == ["good"], "the healthy handler must still run"
    assert len(results) == 2, "emit still reports a slot per handler"


async def test_supervision_is_optional_and_never_blocks_delivery() -> None:
    """A stub watchdog without ``invoke`` must not stop an event.

    Supervision is observability. If it could break delivery it would be a
    liability on the hot path — the fallback keeps the pre-fix behaviour.
    """
    bus = EventBus()
    bus.watchdog = object()  # no invoke()
    seen: list[str] = []

    async def handler(**kwargs: Any) -> None:
        seen.append("ran")

    bus.on("t_stub", handler)
    await bus.emit("t_stub", a=1)

    assert seen == ["ran"]


# ── the per-handler timeout override ────────────────────
#
# ``@subscribe(timeout=...)`` existed and was inert. ``auto_wire`` stamped
# ``meta.timeout`` into the introspection-only ``SubscriptionRegistry`` and
# then called ``watchdog.register(qualname)`` with a single argument, so the
# watchdog fell back to its 5s ``DEFAULT_HANDLER_TIMEOUT_SEC`` for every
# handler in the tree — a declared budget that nothing enforced.
#
# That is what made ``ShortcutService._on_sync_complete`` un-declarable. A
# healthy 1242-game reconcile measured 4.9s against a 5s budget, so any
# contention on the loop pushed it over: the watchdog cancelled the handler
# mid-reconcile twice in one session (at 9.5s and 11.6s — fired *late*,
# itself the signature of a starved loop). A cancelled reconcile leaves
# shortcuts.vdf unwritten and never emits SHORTCUT_RECONCILE_COMPLETE, so
# the user gets neither their shortcuts nor the restart prompt. GOG's 228
# games had no shortcuts for five minutes because of exactly this.


class _TimedService:
    """One handler with a declared budget, one relying on the default."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        auto_wire(self, bus)

    @subscribe(Events.SYNC_COMPLETE, timeout=120.0)
    async def _on_sync_complete(self, **kwargs: Any) -> None: ...

    @subscribe(Events.SYNC_STARTED)
    async def _on_sync_started(self, **kwargs: Any) -> None: ...


def _budget(watchdog: HandlerWatchdog, suffix: str) -> float:
    timeouts = getattr(watchdog, "_timeouts", {})
    name = next(n for n in timeouts if n.endswith(suffix))
    return timeouts[name]


def test_subscribe_timeout_reaches_the_watchdog() -> None:
    bus = EventBus()
    watchdog = HandlerWatchdog()
    bus.watchdog = watchdog
    _TimedService(bus)

    assert _budget(watchdog, "_on_sync_complete") == 120.0


def test_handler_without_an_override_keeps_the_default() -> None:
    """Only declared handlers get a custom budget; the rest are untouched."""
    bus = EventBus()
    watchdog = HandlerWatchdog()
    bus.watchdog = watchdog
    _TimedService(bus)

    timeouts = getattr(watchdog, "_timeouts", {})
    assert not any(n.endswith("_on_sync_started") for n in timeouts)
    # Still registered for metrics, just on the default budget.
    assert any(n.endswith("_on_sync_started") for n in _tracked(watchdog))


def test_the_reconcile_handler_declares_a_realistic_budget() -> None:
    """Pin the actual production declaration, not just the mechanism."""
    from unifideck.services.shortcut.events import (
        RECONCILE_TIMEOUT_SECONDS,
        EventsMixin,
    )
    from unifideck.event_bus.supervision.watchdog_handler import (
        DEFAULT_HANDLER_TIMEOUT_SEC,
    )

    meta = EventsMixin._on_sync_complete.__subscribe_meta__
    assert meta.timeout == RECONCILE_TIMEOUT_SECONDS
    assert RECONCILE_TIMEOUT_SECONDS > DEFAULT_HANDLER_TIMEOUT_SEC, (
        "a full reconcile measured 4.9s against the 5s default"
    )
