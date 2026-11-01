"""What the Battle.net launch popup offers, and its RPC.

tests/unit/test_battlenet_launch_choices.py

The popup lists every ready version of a tile under the client's own name
for it, plus the client itself (added by the frontend). The list is read
from the game's prefix at call time, so a version installed since the last
sync is offered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from unifideck.rpc.mixins.launch import LaunchRPCMixin
from unifideck.stores.battlenet import launch_choices as choices_mod
from unifideck.stores.battlenet.ownership import InstalledGame
from unifideck.stores.battlenet.ownership.pub_catalog import CatalogEntry, MergedCatalog


def _catalog() -> MergedCatalog:
    catalog = MergedCatalog()
    catalog.entries["WoW"] = CatalogEntry(
        product_id="WoW",
        program_id="WoW",
        name_key="world_of_warcraft#WOW_NAME",
        type_uids={"retail": "wow", "wow_classic_era": "wow_classic_era"},
        type_name_keys={"wow_classic_era": "world_of_warcraft#WOW_CLASSIC_NAME"},
    )
    catalog.strings["default"] = {
        "world_of_warcraft#WOW_NAME": "World of Warcraft",
        "world_of_warcraft#WOW_CLASSIC_NAME": "World of Warcraft Classic",
    }
    return catalog


def test_versions_are_named_like_the_client_names_them() -> None:
    catalog = _catalog()
    assert catalog.version_name("wow_classic_era") == "World of Warcraft Classic"
    # Retail has no name of its own and is called by the product's.
    assert catalog.version_name("wow") == "World of Warcraft"
    assert catalog.version_name("unknown") is None


def _row(uid: str) -> InstalledGame:
    return InstalledGame(code=uid, uid=uid, is_ready=True)


def test_choices_list_every_ready_version_with_its_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(choices_mod.paths, "drive_c", lambda prefix: tmp_path)
    monkeypatch.setattr(
        choices_mod, "ready_versions",
        lambda drive_c, prefix, uid: (_catalog(), None, [_row("wow"), _row("wow_classic_era")]),
    )
    assert choices_mod.launch_choices(tmp_path, "wow") == [
        {"uid": "wow", "label": "World of Warcraft"},
        {"uid": "wow_classic_era", "label": "World of Warcraft Classic"},
    ]


def test_an_unknown_prefix_offers_nothing() -> None:
    assert choices_mod.launch_choices(None, "wow") == []


class _Registry:
    def __init__(self, store: Any) -> None:
        self._store = store

    def get_store(self, store_id: str) -> Any:
        return self._store if store_id == "battlenet" else None


class _Store:
    async def launch_choices(self, game_id: str) -> list[dict[str, str]]:
        return [{"uid": game_id, "label": "World of Warcraft"}]


def test_the_rpc_proxies_the_store() -> None:
    mixin = LaunchRPCMixin()
    mixin.registry = _Registry(_Store())
    result = asyncio.run(mixin.get_battlenet_launch_choices("wow"))
    assert result == {"choices": [{"uid": "wow", "label": "World of Warcraft"}]}


def test_the_rpc_answers_empty_without_the_store() -> None:
    mixin = LaunchRPCMixin()
    mixin.registry = _Registry(None)
    assert asyncio.run(mixin.get_battlenet_launch_choices("wow")) == {"choices": []}


def test_the_rpc_accepts_the_store_key_form() -> None:
    """``battlenet:wow`` must reach the store as ``wow``, its id-map key."""
    mixin = LaunchRPCMixin()
    mixin.registry = _Registry(_Store())
    result = asyncio.run(mixin.get_battlenet_launch_choices("battlenet:wow"))
    assert result == {"choices": [{"uid": "wow", "label": "World of Warcraft"}]}


# --------------------------------------------------------------------------
# recording the popup's pick
# --------------------------------------------------------------------------


class _RecordingStore:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    async def record_launch_choice(self, game_id: str, choice: str) -> bool:
        self.recorded.append((game_id, choice))
        return True


def test_the_record_rpc_strips_the_store_prefix() -> None:
    store = _RecordingStore()
    mixin = LaunchRPCMixin()
    mixin.registry = _Registry(store)
    result = asyncio.run(mixin.set_battlenet_launch_choice("battlenet:wow", "client"))
    assert result == {"recorded": True}
    assert store.recorded == [("wow", "client")]


def test_the_record_rpc_refuses_without_the_store() -> None:
    mixin = LaunchRPCMixin()
    mixin.registry = _Registry(None)
    assert asyncio.run(mixin.set_battlenet_launch_choice("wow", "client")) == {"recorded": False}


class _ChoicesOnly:
    """Just enough of BattlenetStore for ``record_launch_choice``."""

    async def launch_choices(self, game_id: str) -> list[dict[str, str]]:
        return [{"uid": "wow", "label": "World of Warcraft"}]


def test_the_store_only_records_installed_versions_or_the_client() -> None:
    from unifideck.stores.battlenet import launch_choice
    from unifideck.stores.battlenet.store import BattlenetStore

    record = BattlenetStore.record_launch_choice
    fake = _ChoicesOnly()
    assert asyncio.run(record(fake, "wow", "wow_classic")) is False  # type: ignore[arg-type]
    assert launch_choice.consume("wow") is None
    assert asyncio.run(record(fake, "wow", "wow")) is True  # type: ignore[arg-type]
    assert launch_choice.consume("wow") == "wow"
    assert asyncio.run(record(fake, "wow", "client")) is True  # type: ignore[arg-type]
    assert launch_choice.consume("wow") == "client"
