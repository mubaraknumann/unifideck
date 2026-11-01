"""Stale launch views and service folders: only ours, only with the lock."""
from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc.sweep import sweep_launch_views, sweep_sessions


def test_removes_only_launch_views_in_this_games_parent(tmp_path: Path) -> None:
    root = tmp_path / "Xbox" / "9PK087LNGJC5"
    game = root / "game"
    game.mkdir(parents=True)
    (root / ".xodus-chain-abc" / "Game.exe").parent.mkdir()
    (root / ".xodus-chain-abc" / "Game.exe").write_bytes(b"MZ")
    (root / ".xodus-offline-1").mkdir()
    (root / ".unifideck-id").write_text("marker")
    other = tmp_path / "Xbox" / "9OTHERGAME12"
    (other / ".xodus-chain-zzz").mkdir(parents=True)
    removed = sweep_launch_views(game, lock_held=True)
    assert removed == [".xodus-chain-abc", ".xodus-offline-1"]
    assert game.is_dir() and (root / ".unifideck-id").is_file()
    assert (other / ".xodus-chain-zzz").is_dir(), "another game's view is never touched"


def test_never_follows_a_symlinked_view(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "game").mkdir(parents=True)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "save.dat").write_text("keep")
    (root / ".xodus-chain-evil").symlink_to(precious, target_is_directory=True)
    assert sweep_launch_views(root / "game", lock_held=True) == []
    assert (precious / "save.dat").read_text() == "keep"


def test_refuses_without_the_game_lock(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        sweep_launch_views(tmp_path / "game", lock_held=False)
    with pytest.raises(RuntimeError):
        sweep_sessions(tmp_path, "9PK087LNGJC5", lock_held=False)


def test_sessions_sweep_only_this_games_folders(tmp_path: Path) -> None:
    (tmp_path / "9PK087LNGJC5-abc").mkdir()
    (tmp_path / "9OTHERGAME12-def").mkdir()
    assert sweep_sessions(tmp_path, "9PK087LNGJC5", lock_held=True) == ["9PK087LNGJC5-abc"]
    assert (tmp_path / "9OTHERGAME12-def").is_dir()
    assert sweep_sessions(tmp_path / "missing", "9PK087LNGJC5", lock_held=True) == []
