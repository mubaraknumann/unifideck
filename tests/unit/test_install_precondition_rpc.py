"""``install_game`` asks the store's ``install_precondition`` before queueing.

The itch.io store answers ``itch_claim_required`` for a bundle game the user
has not claimed: the RPC must return that synchronously (the frontend opens
the claim page on it) and queue nothing.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from unifideck.rpc.mixins.download import DownloadRPCMixin


class _Store:
    def __init__(self, blocked: str | None) -> None:
        self.blocked = blocked

    async def install_precondition(self, game_id: str) -> str | None:
        return self.blocked


class _Downloads:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    async def add(self, **kw: Any) -> Any:
        self.added.append(kw)
        return SimpleNamespace(success=True, error=None)


def _host(store: _Store) -> tuple[DownloadRPCMixin, _Downloads]:
    host = DownloadRPCMixin.__new__(DownloadRPCMixin)
    downloads = _Downloads()
    host.registry = SimpleNamespace(get_store=lambda _name: store)  # type: ignore[attr-defined]
    host.services = SimpleNamespace(download=downloads)  # type: ignore[attr-defined]
    host.config = None  # type: ignore[attr-defined]

    async def no_size(_store: str, _game_id: str) -> None:
        return None

    host._resolve_required_bytes = no_size  # type: ignore[method-assign]
    return host, downloads


async def test_a_blocked_install_is_answered_and_not_queued() -> None:
    host, downloads = _host(_Store("itch_claim_required: claim it first"))
    result = await host.install_game("itch", "2119837", {"storage": "internal"})
    assert result == {"success": False, "error": "itch_claim_required: claim it first"}
    assert downloads.added == []


async def test_an_unblocked_install_is_queued() -> None:
    host, downloads = _host(_Store(None))
    result = await host.install_game("itch", "2119837", {"storage": "internal"})
    assert result["success"] is True
    assert [d["game_id"] for d in downloads.added] == ["2119837"]
