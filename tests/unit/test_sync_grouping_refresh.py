"""Re-grouping after the metadata phase writes Steam mappings.

Duplicate grouping reads ``steam_real_appid``, which the metadata phase
writes after ``sync_complete``. The library is re-grouped when that phase
finishes and when the metadata backfill completes, and only then.
"""
from __future__ import annotations

from typing import Any

from unifideck.core.sync_cache_mixin import _SyncCacheMixin
from unifideck.core.types import Events


class _Bus:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


class _Host(_SyncCacheMixin):
    def __init__(self) -> None:
        self._bus = _Bus()
        self.calls: list[str] = []

    def _annotate_loaded_cache(self) -> None:
        self.calls.append("annotate")

    def _save_library_cache(self) -> None:
        self.calls.append("save")


def _host() -> _Host:
    host = _Host()
    host._subscribe_grouping_refresh()
    return host


def test_metadata_phase_done_regroups_and_saves() -> None:
    host = _host()
    host._bus.handlers[Events.POST_SYNC_PHASE_CHANGED](phase="metadata", active=False)
    assert host.calls == ["annotate", "save"]


def test_other_phases_and_phase_starts_do_nothing() -> None:
    host = _host()
    handler = host._bus.handlers[Events.POST_SYNC_PHASE_CHANGED]
    handler(phase="metadata", active=True)
    handler(phase="artwork", active=False)
    assert host.calls == []


def test_metadata_backfill_regroups() -> None:
    host = _host()
    host._bus.handlers[Events.METADATA_BACKFILL_COMPLETE]()
    assert host.calls == ["annotate", "save"]
