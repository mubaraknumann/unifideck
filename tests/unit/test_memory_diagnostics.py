"""Tests for the self-memory diagnostics that ship in a support bundle.

Two users reported the backend reaching ~22 GB of ``VmData`` while the
plugin sat idle, and no bundle could show it: the bundle described the
machine's memory but never this process's, and a single capture-time
reading cannot separate "always been this size" from "growing 12 MB a
second". These cover the pieces that close that gap — the ``/proc``
parser, the sampling ring, and the block the RPC mixin assembles.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.rpc.mixins.observability import ObservabilityRPCMixin
from unifideck.services.memory_sampler import (
    MIN_INTERVAL_SECONDS,
    MemorySamplerService,
)
from unifideck.services.support_bundle import probe_memory
from unifideck.utils import proc_status

_STATUS_SAMPLE = """\
Name:\tUnifideck
State:\tS (sleeping)
VmPeak:\t  605636 kB
VmSize:\t  574868 kB
VmRSS:\t  178468 kB
RssAnon:\t  158052 kB
VmData:\t  218232 kB
VmSwap:\t   17792 kB
Threads:\t4
voluntary_ctxt_switches:\t105857
nonvoluntary_ctxt_switches:\t18680
"""


@pytest.fixture
def fake_status(tmp_path, monkeypatch):
    """Point the parser at a fixture copy of ``/proc/self/status``."""
    path = tmp_path / "status"
    path.write_text(_STATUS_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(proc_status, "_STATUS_PATH", path)
    return path


class _Host(ObservabilityRPCMixin):
    """Minimal stand-in for the composed Plugin class."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.bus = SimpleNamespace()


class TestProcStatus:
    def test_raw_keeps_the_units_the_kernel_printed(self, fake_status) -> None:
        # A bundle reader must see exactly what /proc said, not a number
        # this code decided to reinterpret.
        raw = proc_status.read_status_raw()
        assert raw["VmData"] == "218232 kB"
        assert raw["Threads"] == "4"

    def test_numeric_strips_units_for_charting(self, fake_status) -> None:
        numeric = proc_status.read_status_numeric()
        assert numeric["VmData"] == 218232
        assert numeric["VmSwap"] == 17792
        assert numeric["Threads"] == 4
        assert numeric["voluntary_ctxt_switches"] == 105857

    def test_unrelated_fields_are_not_carried(self, fake_status) -> None:
        # Name and State are in the file but not in STATUS_KEYS. The block
        # must stay counts-and-sizes only — it goes in a public paste.
        raw = proc_status.read_status_raw()
        assert "Name" not in raw
        assert "State" not in raw

    def test_missing_proc_yields_empty_not_an_exception(
        self, tmp_path, monkeypatch,
    ) -> None:
        # A diagnostic that breaks the thing it is diagnosing is worse
        # than no diagnostic.
        monkeypatch.setattr(proc_status, "_STATUS_PATH", tmp_path / "absent")
        assert proc_status.read_status_raw() == {}
        assert proc_status.read_status_numeric() == {}

    def test_non_numeric_field_is_dropped_not_guessed_at(
        self, tmp_path, monkeypatch,
    ) -> None:
        path = tmp_path / "status"
        path.write_text("VmData:\tnonsense\nThreads:\t4\n", encoding="utf-8")
        monkeypatch.setattr(proc_status, "_STATUS_PATH", path)
        numeric = proc_status.read_status_numeric()
        # Omitted rather than zeroed, so a series never plots a fabricated
        # value that reads as "memory dropped to nothing".
        assert "VmData" not in numeric
        assert numeric["Threads"] == 4


class TestMemorySampler:
    async def test_start_records_a_boot_sample(self, fake_status) -> None:
        svc = MemorySamplerService()
        await svc.start()
        try:
            samples = svc.snapshot()
            assert len(samples) == 1
            assert samples[0]["VmData"] == 218232
            assert "t" in samples[0]
        finally:
            await svc.stop()

    async def test_ring_is_bounded(self, fake_status) -> None:
        # The ring must never become the memory problem it exists to
        # diagnose.
        cfg = _config(memory_sample_max=3)
        svc = MemorySamplerService(config=cfg)
        for _ in range(10):
            svc._sample()
        assert len(svc.snapshot()) == 3

    async def test_interval_is_clamped_to_a_floor(self, fake_status) -> None:
        # Same reason AccountService has a floor: a 0 turns the loop into a
        # spin that allocates while claiming to measure allocation. Note
        # the config schema's ``positiveInt`` is minimum 0 despite its
        # name, so a 0 does reach here from a hand-edited config.
        svc = MemorySamplerService(config=_config(interval=0))
        assert svc._interval == MIN_INTERVAL_SECONDS

    async def test_start_is_idempotent(self, fake_status) -> None:
        svc = MemorySamplerService()
        await svc.start()
        try:
            first = svc._task
            await svc.start()
            assert svc._task is first
        finally:
            await svc.stop()

    async def test_stop_cancels_the_loop(self, fake_status) -> None:
        svc = MemorySamplerService()
        await svc.start()
        task = svc._task
        await svc.stop()
        assert svc._task is None
        assert task is not None
        assert task.done()

    async def test_a_failing_sample_does_not_end_the_series(
        self, fake_status, monkeypatch,
    ) -> None:
        svc = MemorySamplerService()
        # Drive the loop directly rather than waiting out the real floor —
        # the clamp is covered by its own test above.
        svc._interval = 0
        calls: list[int] = []

        def _explode() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("/proc went away")

        monkeypatch.setattr(svc, "_sample", _explode)
        svc._task = asyncio.create_task(svc._loop())
        try:
            # Long enough for the raising tick and at least one after it.
            for _ in range(50):
                await asyncio.sleep(0)
                if len(calls) > 1:
                    break
            assert len(calls) > 1, "loop stopped after one failed sample"
        finally:
            await svc.stop()

    async def test_tracemalloc_stays_off_unless_asked(self, fake_status) -> None:
        import tracemalloc

        was_tracing = tracemalloc.is_tracing()
        svc = MemorySamplerService(config=_config())
        await svc.start()
        try:
            # Tracing roughly doubles allocation cost; it must never be
            # imposed by default.
            assert tracemalloc.is_tracing() is was_tracing
        finally:
            await svc.stop()


class TestProbeMemory:
    def test_gc_histogram_names_types_and_counts_only(self) -> None:
        block = probe_memory.gc_type_histogram(top_n=5)
        assert block["total_objects"] > 0
        assert len(block["types"]) <= 5
        for name, count in block["types"]:
            assert isinstance(name, str)
            assert isinstance(count, int)
            # A module-qualified type name, never an instance repr — the
            # bundle is pasted in public.
            assert "." in name

    def test_histogram_is_ranked_biggest_first(self) -> None:
        block = probe_memory.gc_type_histogram(top_n=10)
        counts = [c for _, c in block["types"]]
        assert counts == sorted(counts, reverse=True)

    def test_tracemalloc_reports_off_rather_than_failing(self) -> None:
        import tracemalloc

        if tracemalloc.is_tracing():
            pytest.skip("tracing already on in this interpreter")
        # The normal case, and not an error.
        assert probe_memory.tracemalloc_top() == {"tracing": False}

    def test_interpreter_block_carries_allocator_facts(self) -> None:
        block = probe_memory.interpreter_block()
        assert isinstance(block["gc_enabled"], bool)
        # Flat allocated_blocks under a growing process means the growth is
        # not in Python objects at all.
        assert block["allocated_blocks"] > 0


class TestObservabilityMemoryBlock:
    def test_block_carries_the_sampler_series(self, fake_status) -> None:
        svc = MemorySamplerService()
        svc._sample()
        host = _Host(SimpleNamespace(memory_sampler=svc))
        block = host._memory_block()
        assert block["samples"][0]["VmData"] == 218232
        assert "gc_types" in block
        assert "interpreter" in block

    def test_block_survives_a_missing_sampler(self) -> None:
        # A bundle from a host whose services failed to come up is exactly
        # when the memory question matters most.
        host = _Host(SimpleNamespace())
        block = host._memory_block()
        assert "samples" not in block
        assert "gc_types" in block

    def test_a_broken_sampler_does_not_cost_the_other_answers(self) -> None:
        def _explode() -> list[dict[str, Any]]:
            raise RuntimeError("sampler down")

        host = _Host(
            SimpleNamespace(memory_sampler=SimpleNamespace(snapshot=_explode)),
        )
        block = host._memory_block()
        assert "samples" not in block
        assert "gc_types" in block


def _config(*, interval: int | None = None, memory_sample_max: int = 720) -> Any:
    """Minimal ConfigManager stand-in for the two keys the sampler reads."""
    values: dict[str, Any] = {
        "diagnostics.memory_sample_interval_seconds": (
            60 if interval is None else interval
        ),
        "diagnostics.memory_sample_max": memory_sample_max,
    }

    return SimpleNamespace(
        get_int=lambda key, default=0: values.get(key, default),
        get_bool=lambda key, default=False: values.get(key, default),
    )
