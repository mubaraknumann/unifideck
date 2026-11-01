"""Tests for ``stores/microsoft/pc_library/installed``: reading the Xbox PC
install records, and which of them count as installed."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.xbox_pc.install_records import write_install_record
from unifideck.launcher.xbox_pc.layout import installed_dir
from unifideck.stores.microsoft.pc_library.installed import (
    find_game_config,
    is_verified,
    load_installed,
)

PID = "9PK087LNGJC5"


@pytest.fixture(autouse=True)
def _data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def make_game(base: Path, pid: str = PID, *, state: str = "installed",
              config_name: str = "MicrosoftGame.config", **over: Any) -> dict[str, Any]:
    """An install on disk plus its record (returned, and written)."""
    root = base / "Xbox" / pid
    game = root / "game"
    game.mkdir(parents=True, exist_ok=True)
    (game / config_name).write_text("<Game/>")
    (game / "Balatro.exe").write_text("MZ")
    record: dict[str, Any] = {
        "schema": 1, "productId": pid, "state": state, "market": "IN",
        "contentId": "1760268b-59d2-488b-8f68-b864a82b23be",
        "installRoot": str(root), "gameDir": str(game), "exe": str(game / "Balatro.exe"),
        "exeRelative": "Balatro.exe", "version": "1.0", "titleId": "1",
        "startedAt": 1, "installedAt": 2,
    }
    record.update(over)
    write_install_record(record)
    return record


def test_an_installed_record_is_verified(tmp_path: Path) -> None:
    make_game(tmp_path / "games")
    state = load_installed()
    assert set(state.records) == {PID} and set(state.verified) == {PID}
    assert state.unreadable == ()


def test_config_name_is_case_insensitive(tmp_path: Path) -> None:
    make_game(tmp_path / "games", config_name="microsoftgame.CONFIG")
    assert find_game_config(tmp_path / "games/Xbox" / PID / "game") is not None
    assert PID in load_installed().verified


def test_partial_records_keep_a_row_but_are_not_installed(tmp_path: Path) -> None:
    make_game(tmp_path / "games", state="partial")
    state = load_installed()
    assert PID in state.records and PID not in state.verified
    assert PID in state.floor_records()


def test_updating_games_are_still_on_disk(tmp_path: Path) -> None:
    assert is_verified(make_game(tmp_path / "games", state="updating"))


def test_relative_exe_is_rejected(tmp_path: Path) -> None:
    assert not is_verified(make_game(tmp_path / "games", exe="game/Balatro.exe"))


def test_missing_config_or_exe_is_not_installed(tmp_path: Path) -> None:
    record = make_game(tmp_path / "games")
    Path(record["exe"]).unlink()
    assert not is_verified(record)
    record = make_game(tmp_path / "other")
    (Path(record["gameDir"]) / "MicrosoftGame.config").unlink()
    assert not is_verified(record)


def test_unmounted_card_keeps_the_record_but_not_installed(tmp_path: Path) -> None:
    record = make_game(tmp_path / "sdcard")
    import shutil

    shutil.rmtree(tmp_path / "sdcard")
    state = load_installed()
    assert PID in state.records and PID not in state.verified
    assert not is_verified(record)


def test_unreadable_records_are_reported_and_left_alone(tmp_path: Path) -> None:
    make_game(tmp_path / "games")
    folder = installed_dir()
    bad = folder / "9NBLGGH4PNC7.json"
    bad.write_text("{torn")
    odd = folder / "notes.json"
    odd.write_text("{}")
    state = load_installed()
    assert set(state.unreadable) == {bad, odd}
    assert state.unreadable_ids() == ["9NBLGGH4PNC7"]
    assert set(state.floor_records()) == {PID, "9NBLGGH4PNC7"}
    # Never deleted or renamed (CLAUDE.md rule 2).
    assert bad.read_text() == "{torn" and odd.exists()
