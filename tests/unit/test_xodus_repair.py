"""Play on an Xbox PC game with missing files repairs it in place.

The launcher's ``not_downloaded`` toast carries ``repair-xbox-pc``; the
frontend calls ``repair_xbox_pc_game``, which queues an install into the
folder the game already has (``xodus/repair.py``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.xbox_pc.errors import REPAIR_VERB, toast_action
from unifideck.rpc.mixins.xbox_pc import XboxPcRPCMixin
from unifideck.stores.microsoft.xodus import repair

PID = "9NMPDQ2NRX34"


def _record(state: str = "installed", root: str | None = None) -> dict[str, Any]:
    return {
        "productId": PID, "state": state, "title": "Stray",
        "installRoot": root if root is not None else f"/run/media/deck/SD/Xbox/{PID}",
    }


@pytest.mark.parametrize("state", ["installed", "partial"])
def test_repair_goes_to_the_base_of_the_existing_install(
    monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    monkeypatch.setattr(repair, "record_for_game", lambda _gid: _record(state))
    assert repair.repair_target(PID) == {"base_path": "/run/media/deck/SD", "title": "Stray"}


@pytest.mark.parametrize(("record", "reason"), [
    (None, "xbox_repair_no_record"),
    (_record("updating"), "xbox_update_pending"),
    (_record(root="relative/Xbox/" + PID), "xbox_repair_no_record"),
    (_record(root="/games/Other/" + PID), "xbox_repair_no_record"),
])
def test_repair_refusals(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, Any] | None, reason: str,
) -> None:
    monkeypatch.setattr(repair, "record_for_game", lambda _gid: record)
    assert repair.repair_target(PID) == {"reason": reason}


def test_not_downloaded_toast_carries_the_repair() -> None:
    assert toast_action("not_downloaded", PID) == {"verb": REPAIR_VERB, "args": [PID]}
    assert toast_action("not_downloaded") is None
    assert toast_action("not_installed", PID) is None


class _Facade:
    def __init__(self, target: dict[str, Any]) -> None:
        self.target = target

    async def repair_target(self, game_id: str) -> dict[str, Any]:
        return self.target


class _Rpc(XboxPcRPCMixin):
    def __init__(self, target: dict[str, Any]) -> None:
        store = type("Store", (), {"xbox_pc": _Facade(target)})()
        self.registry = type("Registry", (), {"get": lambda _self, _name: store})()
        self.queued: list[tuple[Any, ...]] = []

        async def queue(*args: Any) -> dict[str, Any]:
            self.queued.append(args)
            return {"success": True, "error": None}

        self._queue_install = queue


async def test_rpc_queues_the_install_into_the_same_base() -> None:
    rpc = _Rpc({"base_path": "/run/media/deck/SD", "title": "Stray"})
    assert await rpc.repair_xbox_pc_game(PID) == {"success": True, "error": None}
    assert rpc.queued == [("microsoft", PID, "/run/media/deck/SD", "Stray")]


async def test_rpc_reports_a_refusal_without_queueing() -> None:
    rpc = _Rpc({"reason": "xbox_repair_no_record"})
    assert await rpc.repair_xbox_pc_game(PID) == {"success": False, "error": "xbox_repair_no_record"}
    assert rpc.queued == []


def test_base_path_round_trips_through_the_layout() -> None:
    from unifideck.launcher.xbox_pc.layout import install_root_for

    base = Path("/home/deck/Games")
    assert install_root_for(base, PID).parent.parent == base
