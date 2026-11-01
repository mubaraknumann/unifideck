"""Folders that differ only in letter case are merged after a download (I-058)."""
from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.stores.microsoft.xodus.case_folding import CaseConflictError, fold_case_collisions


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_the_minority_spelling_merges_into_the_majority(tmp_path: Path) -> None:
    for name in ("game.exe", "a.dll", "b.dll"):
        _touch(tmp_path / "bin" / "game.x64" / name)
    _touch(tmp_path / "bin" / "Game.x64" / "crash" / "report.exe")

    assert fold_case_collisions(tmp_path) == ["bin/Game.x64"]
    assert _files(tmp_path) == {
        "bin/game.x64/game.exe", "bin/game.x64/a.dll", "bin/game.x64/b.dll",
        "bin/game.x64/crash/report.exe",
    }


def test_nested_spellings_of_the_same_subfolder_merge_too(tmp_path: Path) -> None:
    _touch(tmp_path / "Data" / "maps" / "one.bin")
    _touch(tmp_path / "Data" / "maps" / "two.bin")
    _touch(tmp_path / "data" / "Maps" / "three.bin")

    fold_case_collisions(tmp_path)
    assert _files(tmp_path) == {"Data/maps/one.bin", "Data/maps/two.bin", "Data/maps/three.bin"}


def test_a_tree_without_collisions_is_untouched(tmp_path: Path) -> None:
    _touch(tmp_path / "bin" / "game.exe")
    _touch(tmp_path / "Bin2" / "x.dll")

    assert fold_case_collisions(tmp_path) == []
    assert _files(tmp_path) == {"bin/game.exe", "Bin2/x.dll"}


def test_one_file_name_in_both_spellings_is_a_conflict_and_nothing_moves(tmp_path: Path) -> None:
    _touch(tmp_path / "bin" / "game.exe", "one")
    _touch(tmp_path / "bin" / "other.dll")
    _touch(tmp_path / "Bin" / "Game.exe", "two")
    _touch(tmp_path / "Bin" / "extra.dll")
    before = _files(tmp_path)

    with pytest.raises(CaseConflictError) as caught:
        fold_case_collisions(tmp_path)
    assert caught.value.relative.lower() == "bin/game.exe"
    assert _files(tmp_path) == before


def test_a_link_is_never_followed_or_merged(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _touch(outside / "secret.txt")
    game = tmp_path / "game"
    _touch(game / "bin" / "game.exe")
    (game / "Bin").symlink_to(outside, target_is_directory=True)

    assert fold_case_collisions(game) == []
    assert (outside / "secret.txt").exists()
