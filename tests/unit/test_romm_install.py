"""RomM install layout: normalization, and the shared pipeline over it.

Normalization moves one level. The directory shapes below are the ones the
extractor and the normalizer have to handle, most of them taken from a real
server: an unpacked game delivered as a ZIP with a single wrapping folder, a
flat upload, hidden entries that still count as content, and a wrapper whose
name collides with its child. The rest cover a symlinked wrapper, an empty
directory and a single file.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from unifideck.stores.romm.normalize import normalize_game_directory
from unifideck.stores.shared.archive_install import ArchiveInstaller
from unifideck.stores.shared.archive_source import AcquiredArchive


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


class Source:
    def __init__(self, path: Path, *, prefer_native: bool = False) -> None:
        self.acquired = AcquiredArchive(path=path, title="Wrapped Game", dir_name="Wrapped Game", prefer_native=prefer_native)
        self.released: list[object] = []

    async def acquire(self, game_id, *, progress_callback):
        return self.acquired

    def release(self, acquired):
        self.released.append(acquired)

    async def size(self, game_id):
        return 1


@pytest.fixture(autouse=True)
def isolated_records(tmp_path_factory, monkeypatch):
    scratch = tmp_path_factory.mktemp("romm-state")
    monkeypatch.setenv("XDG_DATA_HOME", str(scratch / "xdg"))
    games_map = scratch / "games.map"
    games_map.write_text("")
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path", lambda config=None: str(games_map))


def relative_files(root: str | None) -> set[str]:
    assert root is not None
    base = Path(root)
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


def installer(tmp_path, source) -> ArchiveInstaller:
    return ArchiveInstaller(
        store="romm", source=source, default_install_root=str(tmp_path / "games"),
        normalize=normalize_game_directory,
    )


async def test_directory_zip_and_uploaded_zip_have_equivalent_layout(tmp_path):
    wrapped = make_zip(tmp_path / "wrapped.zip", {"Wrapped/Game.exe": b"MZ" + b"x" * 2000, "Wrapped/data/file.dat": b"d"})
    flat = make_zip(tmp_path / "flat.zip", {"Game.exe": b"MZ" + b"x" * 2000, "data/file.dat": b"d"})
    first = await installer(tmp_path / "a", Source(wrapped)).install_game("1")
    second = await installer(tmp_path / "b", Source(flat)).install_game("2")
    assert first.success and second.success
    assert relative_files(first.install_path) == relative_files(second.install_path) == {"Game.exe", "data/file.dat"}
    assert first.metadata["exe_path"].endswith("Game.exe")


async def test_native_preference_reaches_resolver(tmp_path):
    archive = make_zip(tmp_path / "g.zip", {"W/Game.exe": b"MZ" + b"x" * 5_000_000, "W/start.sh": b"#!/bin/sh\n"})
    result = await installer(tmp_path, Source(archive, prefer_native=True)).install_game("3")
    assert result.metadata["exe_path"].endswith("start.sh")


async def test_setup_fallback_matches_gamevault(tmp_path):
    archive = make_zip(tmp_path / "g.zip", {"Wrapped/Setup.exe": b"MZ" + b"x" * 2000})
    result = await installer(tmp_path, Source(archive)).install_game("4")
    assert result.success
    assert result.metadata["exe_path"].endswith("Setup.exe")


async def test_plain_exe_has_unknown_format_failure(tmp_path):
    plain = tmp_path / "Game.exe"
    plain.write_bytes(b"MZ" + b"\0" * 100)
    result = await installer(tmp_path, Source(plain)).install_game("5")
    assert result.success is False and result.error


async def test_installed_path_uses_romm_records(tmp_path):
    archive = make_zip(tmp_path / "g.zip", {"Game.exe": b"MZ" + b"x" * 2000})
    inst = installer(tmp_path, Source(archive))
    result = await inst.install_game("6")
    assert inst.get_install_info("6")["install_path"] == result.install_path
    base = Path(os.environ["XDG_DATA_HOME"]) / "unifideck"
    assert (base / "romm_installed" / "6.json").is_file()
    assert not (base / "gamevault_installed").exists()


def test_normalization_is_one_level_only(tmp_path):
    (tmp_path / "outer" / "inner").mkdir(parents=True)
    (tmp_path / "outer" / "inner" / "Game.exe").write_bytes(b"MZfixture")
    normalize_game_directory(tmp_path)
    assert (tmp_path / "inner" / "Game.exe").is_file()
    assert not (tmp_path / "Game.exe").exists()


def test_normalization_preserves_multiple_entries(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    normalize_game_directory(tmp_path)
    assert (tmp_path / "one").is_dir() and (tmp_path / "two").is_dir()


def test_hidden_entry_counts_as_a_second_entry(tmp_path):
    (tmp_path / "wrapper").mkdir()
    (tmp_path / "wrapper" / "Game.exe").write_bytes(b"MZ")
    (tmp_path / ".hidden").write_text("x")
    normalize_game_directory(tmp_path)
    assert (tmp_path / "wrapper" / "Game.exe").is_file()


def test_single_file_is_left_alone(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"MZ")
    normalize_game_directory(tmp_path)
    assert (tmp_path / "Game.exe").is_file()


def test_same_named_child_does_not_collide(tmp_path):
    (tmp_path / "Game" / "Game").mkdir(parents=True)
    (tmp_path / "Game" / "Game" / "file.dat").write_bytes(b"d")
    normalize_game_directory(tmp_path)
    assert (tmp_path / "Game" / "file.dat").is_file()
    assert [p.name for p in tmp_path.iterdir()] == ["Game"]


def test_symlinked_wrapper_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    game = tmp_path / "game"
    game.mkdir()
    (game / "link").symlink_to(outside, target_is_directory=True)
    normalize_game_directory(game)
    assert (outside / "keep.txt").is_file()
    assert (game / "link").is_symlink()


def test_empty_directory_is_a_noop(tmp_path):
    normalize_game_directory(tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_normalization_runs_on_extracted_zip_with_dot_entries(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Wrapped/.config", b"c")
        zf.writestr("Wrapped/Game.exe", b"MZ" + b"x" * 2000)
    archive = tmp_path / "g.zip"
    archive.write_bytes(buf.getvalue())
    result = await installer(tmp_path, Source(archive)).install_game("7")
    assert relative_files(result.install_path) == {".config", "Game.exe"}
