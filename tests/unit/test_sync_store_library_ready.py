"""``STORE_LIBRARY_READY`` queues a sync and returns at once.

A store whose library read outlived the per-store cap (itch.io with a large
library) answers ``None`` so the sync keeps its shortcuts, then emits this
event when the read finishes. The handler must not await the sync itself:
the bus awaits async handlers, so a full sync inside one would hold the
emit for minutes.
"""
from __future__ import annotations

import asyncio
from typing import Any

from unifideck.core.sync_service import SyncService
from unifideck.core.types import Events, SyncResult


class _Bus:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: Events, handler: Any) -> None:
        self.handlers[str(event)] = handler


async def test_library_ready_queues_a_sync_without_awaiting_it() -> None:
    svc = SyncService.__new__(SyncService)
    svc._bus = _Bus()  # type: ignore[assignment]
    started = asyncio.Event()
    release = asyncio.Event()
    sources: list[str] = []

    async def fake_sync_all(**kwargs: Any) -> SyncResult:
        sources.append(kwargs["source"])
        started.set()
        await release.wait()
        return SyncResult(success=True, games=[], count=0, duration_ms=0)

    svc.sync_all = fake_sync_all  # type: ignore[method-assign]
    svc._subscribe_store_library_ready()
    handler = svc._bus.handlers[str(Events.STORE_LIBRARY_READY)]  # type: ignore[attr-defined]

    await asyncio.wait_for(handler(store="itch"), 1)  # returns while the sync runs
    await asyncio.wait_for(started.wait(), 1)
    assert sources == ["library_ready:itch"]
    release.set()
    await asyncio.gather(*svc._library_ready_syncs)
