"""Regression: the library cache must be saved AFTER aggregation.

``_finalize_sync`` used to call ``_save_library_cache()`` *before*
``_aggregate_results()``. ``_aggregate_results`` (via
``_maybe_annotate_duplicate_groups`` / the disabled
``_maybe_collapse_duplicates``) mutates the ``Game`` objects backing
``self._all_games`` in place. Saving first persisted the
pre-aggregation state to ``library_cache.json``, so annotations were
visible in-memory for the rest of that process's life but silently lost
on the next Decky/plugin restart, which reloads straight from that
stale disk cache. This pins the correct order: aggregate, then save.
"""
from __future__ import annotations

import asyncio

import pytest

from unifideck.core import sync_finalize_mixin as m
from unifideck.core.sync_generation import SyncGeneration
from unifideck.core.sync_progress import SyncProgress
from unifideck.core.types import Game, SyncResult
from unifideck.event_bus import EventBus


class _Svc(m._SyncFinalizeMixin):
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._progress = SyncProgress()
        self._all_games: dict[str, list[Game]] = {}
        self._last_sync_time: float | None = None
        self._post_sync_pending: set[str] = set()
        self._registered_phases: set[str] = set()
        self._generation = SyncGeneration()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._cache_snapshot = None
        self.calls: list[str] = []

    def _populate_app_ids(self, libraries: dict[str, list[Game]]) -> None:
        pass

    def _save_library_cache(self) -> None:
        self.calls.append("save")

    def _aggregate_results(
        self,
        libraries: dict[str, list[Game]],
        errors: dict[str, str],
        duration_ms: int,
        total: int,
    ) -> SyncResult:
        self.calls.append("aggregate")
        # Mimic annotate_duplicate_groups: mutate the shared Game
        # objects in place, exactly as the real aggregation step does.
        for games in libraries.values():
            for g in games:
                g.dedupe_group_id = "grouped"
        return SyncResult(success=True, games=[], count=0, duration_ms=duration_ms)


@pytest.mark.asyncio
async def test_aggregate_runs_before_cache_save():
    bus = EventBus()
    svc = _Svc(bus)
    libraries = {
        "epic": [Game(app_id=0, store="epic", store_game_id="a", title="A")],
    }
    try:
        await svc._finalize_sync(libraries, {}, total=1, started=0.0)
    finally:
        if svc._watchdog_task is not None:
            svc._watchdog_task.cancel()

    assert svc.calls == ["aggregate", "save"]
    # The mutation from aggregation must be visible to the save call —
    # this is the actual bug: a "save" that ran first captured None.
    assert libraries["epic"][0].dedupe_group_id == "grouped"
