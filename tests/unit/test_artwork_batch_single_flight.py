"""The artwork batch must be single-flight with replace.

``MetadataService`` and ``CompatibilityService`` both cancel their prior
task when a newer sync arrives. ``ArtworkService`` did not: it assigned
``self._batch_task = fut`` and abandoned whatever was already running.

That single omission drove the whole overlapping-sync cluster measured on
2026-08-29. Three batches ran at once; the stale ones announced the
artwork phase for generations that had been superseded (draining the live
run's pending set, so the Steam-restart modal fired mid-download),
re-triggered CompatibilityService, rewrote shortcut icons from a stale
view, and took two thirds of the shared download semaphore away from the
batch that was current.

It also caused redundant downloads. Each game's on-disk gap is sampled
when its task starts, so a batch sampling while another is still writing
sees gaps that are about to be filled and fetches them again — the
1229-game batch saved 783 covers where only 584 were new.

Two behaviours are pinned here, and the distinction between them is the
subtle part:

* a batch cancelled **by replacement** must stay silent, because the
  replacing batch now owns the artwork phase;
* a batch cancelled by **user cancel** must still announce, or
  ``_post_sync_pending`` keeps ``"artwork"`` forever and the progress bar
  never completes.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from unifideck.services.artwork import event_handlers as eh


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    async def emit(self, event: str, **payload: Any) -> None:
        self.emitted.append({"event": event, **payload})

    def get_sync_progress(self) -> Any:
        return None


class _Artwork(eh._EventHandlersMixin):
    """Host stub exposing only what the batch machinery touches."""

    def __init__(self, bus: Any = None) -> None:
        self._bus = bus
        self._grid_dir = "/grid"
        self._max_concurrent = 10
        self._batch_task: Any = None
        self._batch_state: Any = None
        self.flushes = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.release = asyncio.Event()

    def _flush_artwork_caches(self) -> None:
        self.flushes += 1

    async def _process_one_game(
        self, game: Any, grid_dir: Any, bus: Any, *, force: bool = False,
    ) -> str:
        self.started.append(game.title)
        await self.release.wait()
        self.finished.append(game.title)
        return "cover-saved"


class _Game:
    def __init__(self, title: str) -> None:
        self.title = title
        self.app_id = 1
        self.store = "epic"
        self.store_game_id = title
        self.metadata: dict[str, Any] = {}


async def _settle(fut: Any = None) -> None:
    """Let cancellation, done-callbacks and fire-and-forget emits run.

    ``gather(return_exceptions=True)`` does not leave the gathering future
    cancelled when it is cancelled — each child's CancelledError comes back
    as a result — so the batch resolves normally and its done-callback runs
    a turn later. The phase emit is then itself a fire-and-forget task.
    """
    if fut is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await fut
    for _ in range(6):
        await asyncio.sleep(0)


def _artwork_phase_emits(bus: _Bus) -> list[dict[str, Any]]:
    return [
        e for e in bus.emitted
        if e.get("event") == "post_sync_phase_changed"
        and e.get("phase") == "artwork"
    ]


@pytest.mark.asyncio
async def test_second_batch_cancels_the_first():
    """The defect: a running batch was abandoned, not cancelled."""
    bus = _Bus()
    svc = _Artwork(bus)
    svc._dispatch_artwork_batch(
        [_Game("old")], "/grid", bus, {"run_id": 1}, resync=False,
    )
    await asyncio.sleep(0)
    first = svc._batch_task
    assert not first.done()

    svc._dispatch_artwork_batch(
        [_Game("new")], "/grid", bus, {"run_id": 2}, resync=False,
    )
    await _settle(first)

    assert first.done(), "the replaced batch must not keep running"
    assert svc._batch_task is not first
    assert svc.finished == [], "no cancelled game may complete its fetch"


@pytest.mark.asyncio
async def test_superseded_batch_does_not_announce_the_phase():
    """A replaced batch must not complete the run that replaced it."""
    bus = _Bus()
    svc = _Artwork(bus)
    svc._dispatch_artwork_batch(
        [_Game("old")], "/grid", bus, {"run_id": 1}, resync=False,
    )
    await asyncio.sleep(0)
    svc._dispatch_artwork_batch(
        [_Game("new")], "/grid", bus, {"run_id": 2}, resync=False,
    )
    await _settle(svc._batch_state and None)
    await _settle()

    assert _artwork_phase_emits(bus) == []
    # It still flushes — partial artwork on disk is still valid.
    assert svc.flushes >= 1


@pytest.mark.asyncio
async def test_completed_batch_announces_with_its_run_id():
    bus = _Bus()
    svc = _Artwork(bus)
    svc._dispatch_artwork_batch(
        [_Game("a")], "/grid", bus, {"run_id": 4}, resync=False,
    )
    await asyncio.sleep(0)
    svc.release.set()
    await _settle(svc._batch_task)

    emits = _artwork_phase_emits(bus)
    assert len(emits) == 1
    assert emits[0]["active"] is False
    assert emits[0]["run_id"] == 4


@pytest.mark.asyncio
async def test_user_cancel_still_announces_the_phase():
    """Distinct from replacement: the pending set must still drain."""
    bus = _Bus()
    svc = _Artwork(bus)
    svc._dispatch_artwork_batch(
        [_Game("a")], "/grid", bus, {"run_id": 9}, resync=False,
    )
    await asyncio.sleep(0)

    await svc._on_sync_cancelled()
    await _settle(svc._batch_task)

    emits = _artwork_phase_emits(bus)
    assert len(emits) == 1, "a user-cancelled batch must not strand the phase"
    assert emits[0]["run_id"] == 9


@pytest.mark.asyncio
async def test_batch_is_bounded_not_a_full_fan_out():
    """1242 games must not all start at once.

    Every task opens with ``get_missing_kinds``, which stats up to ten
    filenames, so an unbounded gather queued ~12k stat calls on the same
    loop as the shortcut reconcile — whose watchdog then fired late.
    """
    bus = _Bus()
    svc = _Artwork(bus)
    games = [_Game(f"g{i}") for i in range(200)]
    svc._dispatch_artwork_batch(games, "/grid", bus, {"run_id": 1}, resync=False)
    for _ in range(20):
        await asyncio.sleep(0)

    bound = svc._batch_concurrency()
    assert bound == 30, "3x the configured download concurrency"
    assert len(svc.started) <= bound, (
        f"{len(svc.started)} games started at once against a {bound} bound"
    )

    svc.release.set()
    await svc._batch_task


@pytest.mark.asyncio
async def test_skip_chain_announces_without_doing_work():
    bus = _Bus()
    svc = _Artwork(bus)
    await svc._on_metadata_phase_done(
        phase="metadata",
        active=False,
        sync_kwargs={
            "games": [_Game("a")], "run_id": 3, "skip_chain": True,
        },
    )
    await _settle()

    assert svc.started == []
    emits = _artwork_phase_emits(bus)
    assert len(emits) == 1
    assert emits[0]["run_id"] == 3
