"""The shared archive pipeline: ownership, cancellation and reinstall.

Exercised through a fake ``ArchiveSource`` so these tests cover the pipeline
itself rather than a transport: the store namespace, the normalization hook,
cancellation that must not remove what it did not create, and the library
overlay that must not reintroduce a guessed executable.

The ownership cases are the ones this file exists for: a directory another
store's install owns, a folder holding files no store recorded, the same
folder claimed mid-extraction, and a re-install that must reuse its own
directory rather than acquire a second one.
"""
from __future__ import annotations

import asyncio
import threading
import zipfile
from pathlib import Path

import pytest


class ArchiveSource:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = []

    async def acquire(self, game_id, *, progress_callback):
        from unifideck.stores.shared.archive_source import AcquiredArchive
        return AcquiredArchive(path=self.path, title="My Game", dir_name="My Game")

    def release(self, acquired):
        self.released.append(acquired)

    async def size(self, game_id):
        return 42


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "game.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Game.exe", b"MZfixture")
    return ArchiveSource(path)


@pytest.fixture
def games_map(tmp_path, monkeypatch):
    path = tmp_path / "games.map"
    path.write_text("")
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path", lambda config=None: str(path))
    return path


async def test_installer_uses_store_namespace(tmp_path, source):
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    from unifideck.stores.shared.install_records import InstallRecords
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(tmp_path / "games"))
    result = await installer.install_game("42")
    assert result.success and result.store == "romm"
    assert installer.get_install_info("42")["game_id"] == "42"
    assert InstallRecords("gamevault").load("42") is None
    assert len(source.released) == 1


async def test_normalization_runs_once_in_worker_thread(tmp_path, source):
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    calls = []
    main_thread = threading.get_ident()

    def normalize(path):
        calls.append((path, threading.get_ident()))
        (path / "normalized.txt").write_text("normalized")

    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(tmp_path / "games"), normalize=normalize)
    result = await installer.install_game("42")
    assert result.success
    assert len(calls) == 1
    assert calls[0][1] != main_thread
    assert (Path(result.install_path) / "normalized.txt").read_text() == "normalized"


async def test_exhausted_directory_candidates_does_not_use_foreign_target(tmp_path, source, games_map):
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    root = tmp_path / "games"
    names = ["My Game", "My Game (RomM)"] + [f"My Game (RomM {n})" for n in range(2, 20)]
    games_map.write_text("".join(f"gog:{n}={root / name}/Game.exe\t{root / name}\t-1\n" for n, name in enumerate(names)))
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(root))
    result = await installer.install_game("42")
    assert not result.success
    assert not (root / "My Game" / "Game.exe").exists()
    assert installer.get_install_info("42") is None


async def test_cancel_preserves_new_foreign_owner(tmp_path, source, games_map, monkeypatch):
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    reached = asyncio.Event()
    path = tmp_path / "games" / "My Game"

    async def extract(archive, dest):
        dest.mkdir(parents=True)
        (dest / "foreign.txt").write_text("keep")
        games_map.write_text(f"gog:other={dest}/Game.exe\t{dest}\t-1\n")
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("unifideck.stores.shared.archive_install.extract_archive", extract)
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(path.parent))
    task = asyncio.create_task(installer.install_game("42"))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (path / "foreign.txt").read_text() == "keep"
    assert installer.get_install_info("42") is None


async def test_sync_does_not_reintroduce_guessed_executable(tmp_path, source):
    from unifideck.core.types import Game
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    from unifideck.stores.shared.archive_library import ArchiveLibraryReader
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(tmp_path / "games"))
    await installer.install_game("42")

    class Catalog:
        async def fetch(self):
            return [Game(app_id=0, store="romm", store_game_id="42", title="My Game")]

    game = (await ArchiveLibraryReader(installer, Catalog()).get_library())[0]
    assert game.installed
    assert not game.exe_path
    assert "exe_path" not in game.metadata


async def test_install_skips_a_directory_holding_unowned_files(tmp_path, source):
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    root = tmp_path / "games"
    (root / "My Game").mkdir(parents=True)
    (root / "My Game" / "mine.txt").write_text("keep")
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(root))

    result = await installer.install_game("42")

    assert result.success
    assert result.install_path != str(root / "My Game")
    assert (root / "My Game" / "mine.txt").read_text() == "keep"


async def test_reinstall_reuses_the_games_own_directory(tmp_path, source):
    from unifideck.stores.shared.archive_install import ArchiveInstaller
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(tmp_path / "games"))
    first = await installer.install_game("42")
    second = await installer.install_game("42")
    assert second.install_path == first.install_path
