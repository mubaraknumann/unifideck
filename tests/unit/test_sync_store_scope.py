"""Per-store sync: scoped runs, artwork-only runs, and per-store progress.

The Quick Access store rows sync one store at a time ("Sync games" and
"Sync images" per row) and show each store's own status. Three things
are pinned here:

1. **A scoped run must not cost the other stores anything.** It fetches
   only the stores it names, but ``SYNC_COMPLETE`` still carries the
   whole library — the shortcut reconcile writes shortcuts for every game
   it is handed — and ``stores_synced`` names only the fetched stores, so
   the stale sweep can never reach the others. Getting either half wrong
   deletes a library the user did not touch.
2. **An artwork-only run fetches nothing and sweeps nothing.**
3. **A partial run is never recorded as a full completed chain**, or the
   next whole-library sync would skip work it never did.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.core import sync_service as ss
from unifideck.core.sync_progress import SyncProgress
from unifideck.core.sync_scope import (
    RunScope,
    merge_scoped_libraries,
    normalize_stores,
    scope_of,
    scoped_games,
)
from unifideck.core.types import Events, Game, SyncRequest
from unifideck.event_bus import EventBus


def _games(store: str, count: int) -> list[Game]:
    return [
        Game(app_id=0, store=store, store_game_id=f"{store}-{i}", title=f"{store} {i}")
        for i in range(count)
    ]


# ── scope helpers ───────────────────────────────────────


def test_normalize_stores_keeps_only_store_names():
    assert normalize_stores(["epic", "", 3, "gog"]) == frozenset({"epic", "gog"})


@pytest.mark.parametrize("raw", [None, "epic", b"epic", 5])
def test_normalize_stores_reads_anything_else_as_every_store(raw):
    assert normalize_stores(raw) is None


def test_normalize_stores_keeps_an_empty_list_empty():
    """An empty list names no stores — not the same as naming all of them."""
    assert normalize_stores([]) == frozenset()


def test_scope_of_defaults_to_every_store():
    assert scope_of(None) is None
    assert scope_of({}) is None
    assert scope_of({"scope_stores": None}) is None
    assert scope_of({"scope_stores": "epic"}) is None
    assert scope_of({"scope_stores": ["epic"]}) == frozenset({"epic"})


def test_scoped_games_filters_to_the_payload_scope():
    games = _games("epic", 2) + _games("gog", 3)
    assert scoped_games(games, {"scope_stores": ["gog"]}) == games[2:]
    assert scoped_games(games, {}) is games


def test_merge_keeps_unfetched_stores_and_order():
    cached = {"epic": _games("epic", 2), "gog": _games("gog", 1)}
    fetched = {"gog": _games("gog", 4), "amazon": _games("amazon", 1)}
    merged = merge_scoped_libraries(cached, fetched)
    assert list(merged) == ["epic", "gog", "amazon"]
    assert merged["epic"] is cached["epic"]
    assert merged["gog"] is fetched["gog"]
    assert cached["gog"] != fetched["gog"]  # inputs untouched


def test_run_scope_partiality():
    assert not RunScope().is_partial
    assert RunScope(stores=frozenset({"epic"})).is_partial
    assert RunScope(artwork_only=True).is_partial


# ── request merging ─────────────────────────────────────


def test_merge_unions_store_sets():
    a = SyncRequest(stores=frozenset({"epic"}))
    b = SyncRequest(stores=frozenset({"gog"}))
    assert a.merge(b).stores == frozenset({"epic", "gog"})


def test_merge_every_store_absorbs_a_scope():
    a = SyncRequest(stores=frozenset({"epic"}))
    assert a.merge(SyncRequest()).stores is None
    assert SyncRequest().merge(a).stores is None


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("artwork", "artwork", "artwork"),
        ("artwork", "sync", "sync"),
        ("sync", "artwork", "sync"),
        ("artwork", "force", "force"),
        ("sync", "force", "force"),
    ],
)
def test_merge_kind_precedence(left, right, expected):
    assert SyncRequest(kind=left).merge(SyncRequest(kind=right)).kind == expected


def test_artwork_merged_into_sync_keeps_the_resync():
    art = SyncRequest(kind="artwork", resync_artwork=True, stores=frozenset({"epic"}))
    merged = art.merge(SyncRequest(kind="sync", stores=frozenset({"gog"})))
    assert merged.kind == "sync"
    assert merged.resync_artwork is True
    assert merged.stores == frozenset({"epic", "gog"})


# ── per-store progress rows ─────────────────────────────


def test_rows_queue_then_fetch_then_wait():
    p = SyncProgress()
    p.begin_stores(["epic", "gog"])
    assert {r["state"] for r in p.stores.values()} == {"queued"}
    p.start_store_sync("epic", 0, 2)
    assert p.stores["epic"]["state"] == "active"
    assert p.stores["gog"]["state"] == "queued"
    p.finish_store_fetch("epic", 12, None)
    assert p.stores["epic"] == {
        "state": "waiting", "phase": "games", "done": 12, "total": 12, "error": None,
    }


def test_failed_fetch_stays_failed_through_completion():
    p = SyncProgress()
    p.begin_stores(["epic", "gog"])
    p.finish_store_fetch("epic", 0, "timeout")
    p.finish_store_fetch("gog", 3, None)
    p.begin_store_phase("metadata", _games("gog", 3))
    assert p.stores["epic"]["state"] == "error"
    p.mark_complete()
    assert p.stores["epic"]["state"] == "error"
    assert p.stores["epic"]["error"] == "timeout"
    assert p.stores["gog"]["state"] == "done"


def test_artwork_only_rows_start_waiting():
    p = SyncProgress()
    p.begin_stores(["epic"], fetching=False)
    assert p.stores["epic"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_phase_ticks_each_store_to_its_own_total():
    p = SyncProgress()
    p.begin_stores(["epic", "gog", "amazon"])
    p.begin_store_phase("artwork", _games("epic", 2) + _games("gog", 1))
    assert p.stores["epic"]["total"] == 2
    # A store with nothing in this phase sits it out.
    assert p.stores["amazon"]["state"] == "waiting"
    await p.increment_artwork("e0", "epic")
    assert p.stores["epic"] == {
        "state": "active", "phase": "artwork", "done": 1, "total": 2, "error": None,
    }
    await p.increment_artwork("e1", "epic")
    assert p.stores["epic"]["state"] == "waiting"
    # Ticks without a store, or past the total, change nothing.
    await p.increment_artwork("x")
    await p.increment_artwork("e2", "epic")
    assert p.stores["epic"]["done"] == 2


def test_cancel_marks_unfinished_rows():
    p = SyncProgress()
    p.begin_stores(["epic"])
    p.mark_cancelled()
    assert p.stores["epic"]["state"] == "cancelled"


def test_rows_are_in_the_snapshot_as_copies():
    p = SyncProgress()
    p.begin_stores(["epic"])
    snap = p.to_dict()["stores"]
    snap["epic"]["state"] = "mutated"
    assert p.stores["epic"]["state"] == "queued"


# ── SyncService integration ─────────────────────────────


class _Store:
    def __init__(self, name: str, games: list[Game], available: bool = True) -> None:
        self.store_name = name
        self._games = games
        self._available = available
        self._cached_available = available
        self.fetches: list[bool] = []

    async def is_available(self) -> bool:
        return self._available

    async def get_library(self, force: bool = False) -> list[Game]:
        self.fetches.append(force)
        return list(self._games)


class _Registry:
    def __init__(self, *stores: _Store) -> None:
        self._stores = {s.store_name: s for s in stores}

    def all(self) -> list[_Store]:
        return list(self._stores.values())

    def available(self) -> list[_Store]:
        return [s for s in self._stores.values() if s._cached_available]

    def get_store(self, name: str) -> _Store | None:
        return self._stores.get(name)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """A real SyncService over fake stores, its cache file in ``tmp_path``."""
    monkeypatch.setattr(
        ss.SyncService, "_get_library_cache_path",
        lambda self: tmp_path / "library_cache.json",
    )
    # The 30-minute post-sync watchdog, and the size warm-up a finished
    # chain starts, would both outlive each test's loop.
    monkeypatch.setattr(ss.SyncService, "_arm_watchdog", lambda self: None)
    monkeypatch.setattr(ss.SyncService, "_spawn_size_backfill", lambda self: None)
    epic = _Store("epic", _games("epic", 3))
    gog = _Store("gog", _games("gog", 2))
    bus = EventBus()
    events: dict[str, list[dict[str, Any]]] = {"started": [], "complete": []}

    async def _started(**kw: Any) -> None:
        events["started"].append(kw)

    async def _complete(**kw: Any) -> None:
        events["complete"].append(kw)

    bus.on(Events.SYNC_STARTED, _started)
    bus.on(Events.SYNC_COMPLETE, _complete)
    svc = ss.SyncService(_Registry(epic, gog), bus)
    # No post-sync services here, so no phase would ever report done; with
    # no phases registered each run finishes at its fetch. The queue tests
    # register phases themselves and report them done by hand.
    svc._registered_phases.clear()
    return svc, epic, gog, events


@pytest.mark.asyncio
async def test_scoped_sync_fetches_one_store_and_keeps_the_rest(harness):
    svc, epic, gog, events = harness
    await svc.sync_all()
    assert len(epic.fetches) == len(gog.fetches) == 1
    gog._games = _games("gog", 5)

    await svc.sync_stores(["gog"])

    assert len(epic.fetches) == 1, "epic must not be re-fetched"
    assert gog.fetches[-1] is True, "a named store bypasses its cache"
    done = events["complete"][-1]
    assert done["stores_synced"] == ["gog"], "only gog may be swept"
    assert done["scope_stores"] == ["gog"]
    assert done["artwork_only"] is False
    assert sorted(g.store for g in done["games"]).count("epic") == 3
    assert len(done["games"]) == 8
    assert events["started"][-1]["scope"] == "stores"
    assert svc.store_summary()["gog"]["count"] == 5
    assert svc.store_summary()["epic"]["count"] == 3


@pytest.mark.asyncio
async def test_full_sync_payload_is_unchanged(harness):
    svc, _epic, _gog, events = harness
    await svc.sync_all()
    done = events["complete"][-1]
    assert done["stores_synced"] == ["epic", "gog"]
    assert done["scope_stores"] is None
    assert done["artwork_only"] is False
    assert events["started"][-1]["scope"] == "all"


@pytest.mark.asyncio
async def test_artwork_only_fetches_nothing_and_sweeps_nothing(harness):
    svc, epic, gog, events = harness
    await svc.sync_all()

    await svc.resync_artwork(["epic"])

    assert len(epic.fetches) == len(gog.fetches) == 1
    done = events["complete"][-1]
    assert done["stores_synced"] == []
    assert done["artwork_only"] is True
    assert done["resync_artwork"] is True
    assert done["fetch_artwork"] is True
    assert done["scope_stores"] == ["epic"]
    assert len(done["games"]) == 5
    assert svc._progress.stores["epic"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_scoped_sync_of_an_unavailable_store_announces_nothing(harness):
    svc, _epic, gog, events = harness
    gog._available = False

    result = await svc.sync_stores(["gog"])

    assert result.error == "stores_unavailable"
    assert events["started"] == []
    assert events["complete"] == []


@pytest.mark.asyncio
async def test_store_sync_times_are_stamped_and_persisted(harness):
    svc, _epic, _gog, _events = harness
    await svc.sync_stores(["epic"])
    summary = svc.store_summary()
    assert summary["epic"]["synced_at"] is not None

    reloaded = ss.SyncService(svc._registry, EventBus())
    assert reloaded._store_sync_times == svc._store_sync_times


@pytest.mark.asyncio
async def test_partial_run_is_not_recorded_as_a_full_chain(harness):
    svc, _epic, _gog, _events = harness
    await svc.sync_stores(["epic"])
    svc._record_chain_complete()
    assert not svc._generation.chain_is_redundant(
        frozenset(svc._all_games), sum(len(g) for g in svc._all_games.values()),
    )

    await svc.sync_all()
    svc._record_chain_complete()
    assert svc._generation.chain_is_redundant(
        frozenset(svc._all_games), sum(len(g) for g in svc._all_games.values()),
    )


def test_queued_scope_reports_the_waiting_request(harness):
    svc, _epic, _gog, _events = harness
    assert svc.queued_scope() is None
    svc._pending_request = SyncRequest(kind="force", stores=frozenset({"gog"}))
    assert svc.queued_scope() == {"kind": "force", "stores": ["gog"]}
    svc._pending_request = SyncRequest()
    assert svc.queued_scope() == {"kind": "sync", "stores": None}


def test_status_carries_rows_summary_and_queue(harness):
    svc, _epic, _gog, _events = harness
    status = svc.get_status()
    assert status["stores"] == {}
    assert status["store_summary"] == {}
    assert status["queued"] is None


def test_malformed_sync_times_are_dropped():
    from unifideck.core.sync_cache_mixin import _deserialize_sync_times

    assert _deserialize_sync_times(None) == {}
    assert _deserialize_sync_times({"epic": 1.5, "gog": "x", "amazon": True}) == {
        "epic": 1.5,
    }


# ── queue: one store after another ──────────────────────


async def _until(condition: Any, timeout: float = 5.0) -> None:
    """Yield to the loop until ``condition()`` holds (or fail after timeout)."""
    async def _poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def _finish_chain(svc: ss.SyncService) -> None:
    """Report every post-sync phase done for the current run."""
    for phase in sorted(svc._registered_phases):
        svc._on_post_sync_phase(
            phase=phase, active=False, run_id=svc._generation.run_id,
        )


@pytest.mark.asyncio
async def test_a_queued_store_waits_for_the_previous_run_to_fully_finish(harness):
    """The second store's fetch must not start while the first's chain runs.

    Starting it at the end of the first fetch made the second run's chain
    cancel the first's part-way, leaving the first store half done.
    """
    svc, epic, gog, _events = harness
    svc._registered_phases.update({"metadata", "artwork"})

    first = await svc.sync_stores(["epic"])
    assert first.restart_pending is False, "nothing queued: no wait"
    assert not svc._chain_idle.is_set(), "epic's chain is still running"

    owner = asyncio.create_task(svc.sync_stores(["gog"]))
    await _until(lambda: svc._draining)
    await asyncio.sleep(0.01)
    assert gog.fetches == [], "gog must wait for epic's chain"
    assert svc.queued_scope() == {"kind": "force", "stores": ["gog"]}

    third = await svc.sync_stores(["amazon"])
    assert third.restart_pending is True, "a third press queues behind"

    _finish_chain(svc)
    await _until(lambda: len(gog.fetches) == 1)  # gog runs once epic is done
    await _until(lambda: not svc._chain_idle.is_set())
    _finish_chain(svc)
    await asyncio.wait_for(owner, 1)
    assert svc.queued_scope() is None


@pytest.mark.asyncio
async def test_cancel_drops_the_queue_and_releases_it(harness):
    svc, _epic, gog, _events = harness
    svc._registered_phases.update({"metadata", "artwork"})
    await svc.sync_stores(["epic"])
    owner = asyncio.create_task(svc.sync_stores(["gog"]))
    await _until(lambda: svc._draining)

    assert await svc.cancel() is True, "cancel reaches the post-sync chain"
    await asyncio.wait_for(owner, 1)
    assert gog.fetches == [], "a cancelled queue does not run on"
    assert svc.queued_scope() is None
    assert await svc.cancel() is False, "nothing left to cancel"
