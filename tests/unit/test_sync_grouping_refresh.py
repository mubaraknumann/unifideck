"""Re-grouping after the metadata phase writes Steam mappings.

Duplicate grouping reads ``steam_real_appid``, which the metadata phase
writes after ``sync_complete``. The library is re-grouped when that phase
finishes and when the metadata backfill completes, and only then.

The refresh itself must not race the rest of the service: it computes on
copies off the loop, applies on the loop, never overwrites a library that a
sync or a reset replaced meanwhile, and runs one at a time.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from unifideck.core import game_grouping
from unifideck.core.sync_cache_mixin import _SyncCacheMixin
from unifideck.core.types import Events
from unifideck.core.types.domain import Game


class _Bus:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


class _Host(_SyncCacheMixin):
    def __init__(self) -> None:
        self._bus = _Bus()
        self._config = None
        self._all_games: dict[str, list[Game]] = {}
        self.calls: list[str] = []
        self.saved: list[dict[str, list[Game]]] = []

    def _save_library_cache(self) -> None:
        self.calls.append("save")
        self.saved.append(self._all_games)


class _RecordingHost(_Host):
    async def refresh_duplicate_groups(self) -> None:
        self.calls.append("refresh")


def _subscribed(host: _Host) -> _Host:
    host._subscribe_grouping_refresh()
    return host


async def test_metadata_phase_done_regroups() -> None:
    host = _subscribed(_RecordingHost())
    await host._bus.handlers[Events.POST_SYNC_PHASE_CHANGED](phase="metadata", active=False)
    assert host.calls == ["refresh"]


async def test_other_phases_and_phase_starts_do_nothing() -> None:
    host = _subscribed(_RecordingHost())
    handler = host._bus.handlers[Events.POST_SYNC_PHASE_CHANGED]
    await handler(phase="metadata", active=True)
    await handler(phase="artwork", active=False)
    assert host.calls == []


async def test_metadata_backfill_regroups() -> None:
    host = _subscribed(_RecordingHost())
    await host._bus.handlers[Events.METADATA_BACKFILL_COMPLETE]()
    assert host.calls == ["refresh"]


def test_bus_handlers_are_coroutines() -> None:
    """A sync handler runs on the bus's thread pool, concurrently with the
    owned-titles RPC's refresh; a coroutine runs on the loop."""
    host = _subscribed(_Host())
    for handler in host._bus.handlers.values():
        assert asyncio.iscoroutinefunction(handler)


def _game(app_id: int, title: str) -> Game:
    return Game(app_id=app_id, store="epic", store_game_id=str(app_id), title=title)


def _library(host: _Host) -> list[Game]:
    games = [_game(-1, "Hades"), _game(-2, "Celeste")]
    host._all_games = {"epic": games}
    return games


async def test_refresh_applies_the_new_grouping_and_saves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    live = _library(host)
    seen: list[list[Game]] = []

    def annotate(games: list[Game], *_a: Any) -> list[Game]:
        seen.append(games)
        for g in games:
            g.dedupe_group_id = f"group:{g.title}"
        return games

    monkeypatch.setattr(game_grouping, "annotate_duplicate_groups_if_enabled", annotate)
    await host.refresh_duplicate_groups()

    assert [g.dedupe_group_id for g in live] == ["group:Hades", "group:Celeste"]
    assert host.calls == ["save"]
    # It worked on copies: the live objects were never handed to the thread.
    assert not any(c is g for c in seen[0] for g in live)


async def test_live_games_are_untouched_while_the_grouping_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    live = _library(host)
    for g in live:
        g.dedupe_group_id = "old"
    during: list[list[str | None]] = []

    def annotate(games: list[Game], *_a: Any) -> list[Game]:
        for g in games:
            g.dedupe_group_id = None  # the real pass clears first
        during.append([g.dedupe_group_id for g in live])
        for g in games:
            g.dedupe_group_id = "new"
        return games

    monkeypatch.setattr(game_grouping, "annotate_duplicate_groups_if_enabled", annotate)
    await host.refresh_duplicate_groups()

    assert during == [["old", "old"]]  # a reader mid-pass saw consistent data
    assert [g.dedupe_group_id for g in live] == ["new", "new"]


async def test_a_library_replaced_meanwhile_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    stale = _library(host)
    fresh = [_game(-3, "Hollow Knight")]

    def annotate(games: list[Game], *_a: Any) -> list[Game]:
        host._all_games = {"epic": fresh}  # a sync finalize landed meanwhile
        for g in games:
            g.dedupe_group_id = "from-the-old-library"
        return games

    monkeypatch.setattr(game_grouping, "annotate_duplicate_groups_if_enabled", annotate)
    await host.refresh_duplicate_groups()

    assert host.calls == []  # nothing saved over the newer library
    assert all(g.dedupe_group_id is None for g in stale + fresh)


async def test_concurrent_refreshes_run_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    _library(host)
    active = 0
    peak = 0
    lock = threading.Lock()

    def annotate(games: list[Game], *_a: Any) -> list[Game]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.05)
        with lock:
            active -= 1
        return games

    monkeypatch.setattr(game_grouping, "annotate_duplicate_groups_if_enabled", annotate)
    await asyncio.gather(*(host.refresh_duplicate_groups() for _ in range(3)))

    assert peak == 1
    assert host.calls == ["save", "save", "save"]


async def test_a_failed_grouping_keeps_the_library_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    live = _library(host)

    def annotate(*_a: Any) -> list[Game]:
        raise RuntimeError("bad mapping")

    monkeypatch.setattr(game_grouping, "annotate_duplicate_groups_if_enabled", annotate)
    await host.refresh_duplicate_groups()

    assert host.calls == []
    assert all(g.dedupe_group_id is None for g in live)
