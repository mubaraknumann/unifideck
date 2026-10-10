"""Regression: a queued sync request logs how long the lock has been held.

UD-013: after a store login, both auto-sync-on-login (``request_auth_sync``)
and a manual "Sync" click appeared to silently do nothing until a full Steam
restart. The suspected mechanism is ``SyncService._lock`` getting wedged
forever by a stuck handler inside an ``EventBus.emit`` fan-out (see
``tests/event_bus/test_event_bus.py`` for that fix) — every subsequent
``sync_all``/``request_auth_sync`` call then queues behind a lock that never
releases. ``_enqueue`` already logged "queued behind in-flight" whenever a
caller found the lock held, but didn't say for how long — the one fact that
distinguishes a merely slow legitimate sync from a permanently stuck one.
This test locks in that the held duration is now included.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from unifideck.core.sync_service import SyncService
from unifideck.core.types import SyncRequest, SyncResult


def _bare_service() -> SyncService:
    """A ``SyncService`` with only the state ``_enqueue`` touches.

    Bypasses ``__init__`` (which needs a real ``StoreRegistry``/``EventBus``)
    so this stays a focused unit test of the lock-bookkeeping in ``_enqueue``
    itself, mirroring ``tests/unit/test_sync_store_timeout.py``'s pattern of
    exercising the real method against a minimal stand-in.
    """
    svc = SyncService.__new__(SyncService)
    svc._lock = asyncio.Lock()
    svc._lock_acquired_at = None
    svc._request_lock = asyncio.Lock()
    svc._pending_request = None
    svc._draining = False
    svc._chain_idle = asyncio.Event()
    svc._chain_idle.set()
    return svc


@pytest.mark.asyncio
async def test_queued_request_logs_held_duration(caplog, monkeypatch):
    svc = _bare_service()
    release_hold = asyncio.Event()

    async def _fake_run_sync(**_kwargs: object) -> SyncResult:
        await release_hold.wait()
        return SyncResult(success=True, games=[], count=0, duration_ms=0)

    monkeypatch.setattr(svc, "_run_sync", _fake_run_sync)

    holder_task = asyncio.create_task(
        svc._enqueue(SyncRequest(kind="sync", source="manual")),
    )
    # Let the holder actually acquire the lock before the second request
    # queues behind it.
    for _ in range(100):
        if svc._lock.locked():
            break
        await asyncio.sleep(0.01)
    assert svc._lock.locked()
    assert svc._lock_acquired_at is not None

    await asyncio.sleep(0.2)
    with caplog.at_level("INFO"):
        result = await svc._enqueue(SyncRequest(kind="sync", source="auth:gog"))
    assert result.restart_pending is True

    release_hold.set()
    await holder_task

    queued_logs = [
        r.message for r in caplog.records if "queued behind in-flight" in r.message
    ]
    assert len(queued_logs) == 1
    assert "held=" in queued_logs[0]
    assert "held=unknown" not in queued_logs[0]


@pytest.mark.asyncio
async def test_lock_acquired_at_clears_after_release():
    svc = _bare_service()

    async def _fast_run_sync(**_kwargs: object) -> SyncResult:
        assert svc._lock_acquired_at is not None
        return SyncResult(success=True, games=[], count=0, duration_ms=0)

    svc._run_sync = _fast_run_sync
    await svc._enqueue(SyncRequest(kind="sync", source="manual"))
    assert svc._lock_acquired_at is None
    assert not svc._lock.locked()
