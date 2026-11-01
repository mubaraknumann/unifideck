"""Tests for the ``get_game_info`` RPC during and after boot.

Decky serves RPCs while ``_main`` is still running, so a game page left
open across a plugin restart can call ``get_game_info`` before
``boot_plugin`` has set ``sync_service``. That call used to raise
``AttributeError``, which the frontend cached as "no data" and never
retried, leaving the page on the loading skeleton. It must instead fail
with a retryable ``service_unavailable`` error, and never answer
``None``, which the frontend reads as "not a Unifideck game".

The mixin is exercised unwrapped (``@auto_wrap_rpc_methods`` sits on the
``Plugin`` class), so assertions are on the raw coroutine.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.rpc import RpcError
from unifideck.rpc.mixins.sync import SyncRPCMixin


class _FakeSyncService:
    def get_game_info(self, app_id: int) -> dict[str, Any] | None:
        return {"app_id": app_id, "store": "gog"} if app_id == 7 else None


def test_get_game_info_before_boot_raises_service_unavailable() -> None:
    host = SyncRPCMixin()  # no sync_service attribute at all, as mid-boot

    with pytest.raises(RpcError) as excinfo:
        asyncio.run(host.get_game_info(7))

    assert excinfo.value.code == "service_unavailable"


def test_get_game_info_with_sync_service_none_raises() -> None:
    host = SyncRPCMixin()
    host.sync_service = None

    with pytest.raises(RpcError):
        asyncio.run(host.get_game_info(7))


def test_get_game_info_after_boot_returns_the_record() -> None:
    host = SyncRPCMixin()
    host.sync_service = _FakeSyncService()

    assert asyncio.run(host.get_game_info(7)) == {"app_id": 7, "store": "gog"}
    assert asyncio.run(host.get_game_info(8)) is None
