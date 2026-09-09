"""Two shutdown/idle-cost regressions found while hunting a memory leak.

1. ``EventReplayBuffer.snapshot`` had no ``since``, so the frontend's 2s
   poll made the backend re-serialise the entire replay buffer forever —
   and ``rpc.wrapper._serialize`` deep-copies every dataclass on the way
   out. The frontend was already discarding almost all of it client-side.

2. ``stop_all_services`` walked a hand-maintained list that had drifted
   from ``ServiceContainer``. It omitted nine services (including
   ``compatibility``, whose enrichment task was therefore never
   cancelled) and named a ``cloud_prompt`` that has never been a field.
"""
from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.core.types import Events
from unifideck.event_bus.event_replay import EventReplayBuffer
from unifideck.rpc.mixins.observability import ObservabilityRPCMixin
from unifideck.services.bootstrap.container import ServiceContainer
from unifideck.services.bootstrap.teardown import stop_all_services, teardown_order


class _Host(ObservabilityRPCMixin):
    """Minimal stand-in for the composed Plugin class."""

    def __init__(self, replay: Any) -> None:
        self.replay = replay
        self.services = SimpleNamespace()
        self.bus = SimpleNamespace()


@pytest.fixture
def tick(monkeypatch):
    """Give ``record`` a controlled clock.

    ``to_dict`` rounds to milliseconds, so three records written in a
    tight loop genuinely share one timestamp. Real emissions are spread
    out; these tests need distinct ones to say anything about ordering.
    """
    import unifideck.event_bus.event_replay as mod

    counter = {"t": 1000.0}

    def _monotonic() -> float:
        counter["t"] += 1.0
        return counter["t"]

    monkeypatch.setattr(mod.time, "monotonic", _monotonic)
    return counter


class TestSnapshotWatermark:
    def _buffer(self) -> EventReplayBuffer:
        buf = EventReplayBuffer()
        for i in range(3):
            buf.record(Events.SYNC_PROGRESS, {"n": i})
        return buf

    def test_no_watermark_returns_everything(self, tick) -> None:
        # A caller with no watermark yet (a fresh frontend load) depends on
        # receiving the whole backlog so it can prime past stale events.
        assert len(self._buffer().snapshot()) == 3

    def test_watermark_excludes_everything_already_seen(self, tick) -> None:
        buf = self._buffer()
        newest = buf.snapshot()[0]["timestamp"]
        # The steady-state answer on an idle plugin: nothing new. This is
        # the whole point — it used to be the entire buffer, twice a second.
        assert buf.snapshot(since=newest) == []

    def test_watermark_returns_only_newer_records(self, tick) -> None:
        buf = self._buffer()
        oldest = buf.snapshot()[-1]["timestamp"]
        fresh = buf.snapshot(since=oldest)
        assert len(fresh) == 2
        assert all(r["timestamp"] > oldest for r in fresh)

    def test_watermark_is_exclusive(self, tick) -> None:
        # Inclusive would re-dispatch the last event on every single poll.
        buf = EventReplayBuffer()
        buf.record(Events.SYNC_PROGRESS, {"n": 0})
        ts = buf.snapshot()[0]["timestamp"]
        assert buf.snapshot(since=ts) == []

    def test_watermark_composes_with_the_event_filter(self, tick) -> None:
        buf = EventReplayBuffer()
        buf.record(Events.SYNC_PROGRESS, {"n": 0})
        buf.record(Events.DOWNLOAD_PROGRESS, {"n": 1})
        everything = buf.snapshot()
        oldest = everything[-1]["timestamp"]
        fresh = buf.snapshot(events=[Events.DOWNLOAD_PROGRESS], since=oldest)
        assert [r["event"] for r in fresh] == [Events.DOWNLOAD_PROGRESS.value]

    async def test_rpc_forwards_the_watermark(self) -> None:
        seen: dict[str, Any] = {}

        def _snapshot(events: Any = None, since: Any = None) -> list[Any]:
            seen["events"] = events
            seen["since"] = since
            return []

        host = _Host(SimpleNamespace(snapshot=_snapshot))
        await host.subscribe_replay(["sync_progress"], 42.5)
        assert seen["since"] == 42.5

    async def test_rpc_watermark_is_optional(self) -> None:
        # An older frontend bundle calls with one argument and must still
        # get the full buffer rather than an error.
        seen: dict[str, Any] = {}

        def _snapshot(events: Any = None, since: Any = None) -> list[Any]:
            seen["since"] = since
            return []

        host = _Host(SimpleNamespace(snapshot=_snapshot))
        await host.subscribe_replay(["sync_progress"])
        assert seen["since"] is None


class _Stoppable:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class TestTeardownOrder:
    def test_covers_every_container_field(self) -> None:
        # The property the old hand-written list failed to hold. Deriving
        # the order means a service added to the container cannot be
        # silently skipped at unload.
        assert set(teardown_order(ServiceContainer())) == {
            f.name for f in fields(ServiceContainer)
        }

    def test_names_no_field_that_does_not_exist(self) -> None:
        # The old list carried a "cloud_prompt" that was never a field.
        known = {f.name for f in fields(ServiceContainer)}
        assert [n for n in teardown_order(ServiceContainer()) if n not in known] == []

    def test_is_reverse_construction_order(self) -> None:
        # A service is stopped before whatever it was built on top of.
        assert teardown_order(ServiceContainer()) == [
            f.name for f in reversed(fields(ServiceContainer))
        ]

    @pytest.mark.parametrize(
        "attr",
        [
            "compatibility",
            "activity_log",
            "launch_history",
            "launch_logs",
            "support_bundle",
            "microsoft_subscription",
            "user_paths_coordinator",
            "memory_sampler",
        ],
    )
    async def test_previously_forgotten_services_are_stopped(self, attr: str) -> None:
        # Each of these was absent from the hardcoded list. compatibility is
        # the one that mattered most: its _enrichment_task outlived unload.
        svc = _Stoppable()
        container = ServiceContainer(**{attr: svc})  # type: ignore[arg-type]
        await stop_all_services(container)
        assert svc.stopped, f"{attr} was not stopped"

    async def test_a_raising_stop_does_not_strand_the_rest(self) -> None:
        class _Angry:
            async def stop(self) -> None:
                raise RuntimeError("already gone")

        later = _Stoppable()
        # shortcut is stopped last, so a failure anywhere before it must
        # not prevent it.
        container = ServiceContainer(
            security=_Angry(),  # type: ignore[arg-type]
            shortcut=later,  # type: ignore[arg-type]
        )
        await stop_all_services(container)
        assert later.stopped

    async def test_a_service_without_stop_is_skipped(self) -> None:
        container = ServiceContainer(shortcut=SimpleNamespace())  # type: ignore[arg-type]
        await stop_all_services(container)  # must not raise

    async def test_disconnect_is_accepted_for_the_cdp_client(self) -> None:
        calls: list[str] = []

        async def _disconnect() -> None:
            calls.append("disconnect")

        container = ServiceContainer(
            cdp=SimpleNamespace(disconnect=_disconnect),  # type: ignore[arg-type]
        )
        await stop_all_services(container)
        assert calls == ["disconnect"]
