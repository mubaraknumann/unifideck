"""Install records: the on-disk contract, and the paths they refuse.

The GameVault case is here because these records replaced
``stores/gamevault/markers.py`` in place: markers written before the move must
still be readable, and the layout must not change with it.
"""
from __future__ import annotations

import json

import pytest


def test_existing_gamevault_marker_is_readable(tmp_path, monkeypatch):
    from unifideck.stores.shared.install_records import InstallRecords
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    marker = {"game_id": "lv_123", "title": "Old Game", "install_path": "/games/old", "exe_path": "/games/old/Game.exe", "archive_path": "/vault/old.zip"}
    root = tmp_path / "unifideck" / "gamevault_installed"
    root.mkdir(parents=True)
    (root / "lv_123.json").write_text(json.dumps(marker))
    assert InstallRecords("gamevault").load("lv_123") == marker


def test_store_records_are_separate_and_resolve_environment_at_call_time(tmp_path, monkeypatch):
    from unifideck.stores.shared.install_records import InstallRecords
    records = InstallRecords("romm")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "first"))
    records.save("42", title="Game", install_path="/games/game", exe_path="/games/game/Game.exe")
    assert records.load("42")["archive_path"] == ""
    assert InstallRecords("gamevault").load_all() == {}
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "second"))
    assert records.load("42") is None


@pytest.mark.parametrize("game_id", ["../outside", "..", "/absolute", "a/b"])
def test_record_id_cannot_escape_namespace(game_id):
    from unifideck.stores.shared.install_records import InstallRecords
    with pytest.raises(ValueError):
        InstallRecords("romm").save(game_id, title="Game", install_path="/games/game", exe_path="")
