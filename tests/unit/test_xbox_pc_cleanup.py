"""Cleanup and Xbox PC installs.

"Delete all data" without deleting games must keep ``xbox_pc/`` (the install
records index multi-GB games, and the runtime is 1.5 GB). The destructive
sweep must find every Xbox PC install through its record, ``partial`` ones
included, so the marked ``installRoot`` is deleted with its launch views.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.core import marker_sweep
from unifideck.launcher.xbox_pc.install_records import write_install_record
from unifideck.rpc.mixins.sync import SyncRPCMixin


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return home


def _record(pid: str, root: Path, state: str = "installed") -> dict[str, Any]:
    record = {"schema": 1, "productId": pid, "state": state, "market": "IN",
              "installRoot": str(root), "gameDir": str(root / "game"),
              "exe": str(root / "game" / "a.exe")}
    write_install_record(record)
    return record


def _marked(root: Path, pid: str) -> None:
    (root / "game").mkdir(parents=True)
    (root / ".xodus-chain-1").mkdir()
    (root / ".unifideck_manifest.json").write_text(json.dumps({"store": "microsoft", "store_id": pid}))


async def test_keeping_games_keeps_the_xbox_pc_folder(home: Path) -> None:
    data = home / ".local/share/unifideck"
    (data / "xbox_pc/installed").mkdir(parents=True)
    (data / "xbox_pc/installed/9PK087LNGJC5.json").write_text("{}")
    (data / "library_cache.json").write_text("{}")

    await SyncRPCMixin()._wipe_data_dir(delete_files=False)

    assert (data / "xbox_pc/installed/9PK087LNGJC5.json").exists()
    assert not (data / "library_cache.json").exists()


async def test_destructive_cleanup_removes_the_xbox_pc_folder(home: Path) -> None:
    data = home / ".local/share/unifideck"
    (data / "xbox_pc/installed").mkdir(parents=True)

    await SyncRPCMixin()._wipe_data_dir(delete_files=True)

    assert not (data / "xbox_pc").exists()


def test_install_records_give_their_library_roots(home: Path) -> None:
    sd = home / "sdcard/Games/Xbox/9PK087LNGJC5"
    internal = home / "Games/Xbox/9MVQN16MCT10"
    _marked(sd, "9PK087LNGJC5")
    _marked(internal, "9MVQN16MCT10")
    _record("9PK087LNGJC5", sd)
    _record("9MVQN16MCT10", internal, state="partial")

    roots = marker_sweep.collect_install_roots()

    assert {home / "sdcard/Games/Xbox", home / "Games/Xbox"} <= roots
    assert marker_sweep.find_for_game(roots, "microsoft", "9PK087LNGJC5") == sd
    assert marker_sweep.sweep_all(roots) == 2
    assert not sd.exists() and not internal.exists()


def test_a_relative_install_root_is_ignored(home: Path) -> None:
    _record("9PK087LNGJC5", Path("Games/Xbox/9PK087LNGJC5"))
    assert marker_sweep.collect_install_roots() == set()
