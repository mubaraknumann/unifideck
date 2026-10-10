"""The post-sync phases work through only the stores a run covers.

``SYNC_COMPLETE`` always carries the whole library (the shortcut
reconcile needs it), so each phase narrows ``games`` to ``scope_stores``
itself — otherwise "Sync games" on one store would re-run metadata,
artwork and compat over every store's games. An artwork-only run must
also leave metadata and compat alone, and a scoped artwork resync must
not wipe the other stores' attempt records.

The handlers are called unbound on small stubs: each test pins what a
handler hands to its worker, not the worker itself.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.core.sync_progress import SyncProgress
from unifideck.core.types import Game
from unifideck.services.artwork import event_handlers as eh
from unifideck.services.compatibility.service import CompatibilityService
from unifideck.services.metadata_service import MetadataService


def _library() -> list[Game]:
    return [
        Game(app_id=1, store="epic", store_game_id="e", title="E"),
        Game(app_id=2, store="gog", store_game_id="g", title="G"),
    ]


def _recorder() -> tuple[SimpleNamespace, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def _run_enrichment(games: list[Game], **kw: Any) -> None:
        calls.append({"games": games, **kw})

    return SimpleNamespace(_enrichment_task=None, _run_enrichment=_run_enrichment), calls


# ── metadata ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metadata_walks_only_the_scoped_stores():
    stub, calls = _recorder()
    await MetadataService._on_sync_complete(
        stub, games=_library(), scope_stores=["gog"], artwork_only=False,
    )
    await stub._enrichment_task
    assert [g.store for g in calls[0]["games"]] == ["gog"]
    assert calls[0]["skip"] is False


@pytest.mark.asyncio
async def test_metadata_walks_everything_without_a_scope():
    stub, calls = _recorder()
    await MetadataService._on_sync_complete(stub, games=_library())
    await stub._enrichment_task
    assert len(calls[0]["games"]) == 2


@pytest.mark.asyncio
async def test_metadata_stands_aside_for_artwork_only():
    stub, calls = _recorder()
    await MetadataService._on_sync_complete(
        stub, games=_library(), artwork_only=True,
    )
    await stub._enrichment_task
    assert calls[0]["skip"] is True


# ── compat ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compat_walks_only_the_scoped_stores_and_skips_artwork_only():
    stub, calls = _recorder()
    await CompatibilityService._on_artwork_phase_done(
        stub, phase="artwork", active=False,
        sync_kwargs={"games": _library(), "scope_stores": ["epic"]},
    )
    await stub._enrichment_task
    assert [g.store for g in calls[0]["games"]] == ["epic"]
    assert calls[0]["skip"] is False

    await CompatibilityService._on_artwork_phase_done(
        stub, phase="artwork", active=False,
        sync_kwargs={"games": _library(), "artwork_only": True},
    )
    await stub._enrichment_task
    assert calls[1]["skip"] is True


# ── artwork ─────────────────────────────────────────────


class _Bus:
    def __init__(self) -> None:
        self.progress = SyncProgress()
        self.progress.begin_stores(["epic", "gog"])

    def get_sync_progress(self) -> SyncProgress:
        return self.progress

    async def emit(self, *_a: Any, **_k: Any) -> None:
        return None


class _Artwork(eh._EventHandlersMixin):
    def __init__(self) -> None:
        self._bus = _Bus()
        self._grid_dir = "/grid"
        self.cleared = 0
        self.dispatched: list[tuple[list[Game], bool]] = []

    def _clear_resync_cache(self) -> None:
        self.cleared += 1

    def _dispatch_artwork_batch(
        self, games: list[Game], grid_dir: Any, bus: Any,
        sync_kwargs: dict[str, Any], *, resync: bool,
    ) -> None:
        self.dispatched.append((games, resync))


async def _artwork_phase(svc: _Artwork, **sync_kwargs: Any) -> None:
    await svc._on_metadata_phase_done(
        phase="metadata", active=False,
        sync_kwargs={"games": _library(), **sync_kwargs},
    )


@pytest.mark.asyncio
async def test_scoped_resync_keeps_the_other_stores_attempt_records():
    svc = _Artwork()
    await _artwork_phase(svc, scope_stores=["gog"], resync_artwork=True)
    assert svc.cleared == 0
    games, resync = svc.dispatched[0]
    assert [g.store for g in games] == ["gog"]
    assert resync is True
    rows = svc._bus.progress.stores
    assert rows["gog"]["phase"] == "artwork"
    assert rows["gog"]["total"] == 1
    # epic is outside the scope: no games in this phase.
    assert rows["epic"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_full_resync_still_clears_every_attempt_record():
    svc = _Artwork()
    await _artwork_phase(svc, resync_artwork=True)
    assert svc.cleared == 1
    assert len(svc.dispatched[0][0]) == 2


def test_handlers_are_plain_coroutines():
    """The unbound calls above rely on ``@subscribe`` returning the function."""
    assert inspect.iscoroutinefunction(MetadataService._on_sync_complete)
    assert inspect.iscoroutinefunction(CompatibilityService._on_artwork_phase_done)
