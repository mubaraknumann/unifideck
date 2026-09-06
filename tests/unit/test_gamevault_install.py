"""Tests for ``stores.gamevault.install`` and ``.archive``.

Covers the shared install pipeline, archive-format detection and the install
marker. The pieces that used to live here and now have their own files:
executable scoring → ``test_gamevault_exe_finder.py``; the download and
``Content-Disposition`` handling → ``test_gamevault_sources.py``.

The pipeline is deliberately exercised through a fake ``ArchiveSource``
rather than through either real one. That is the shape of the design: the
installer is not supposed to know whether it is unpacking a download or a
file the user already had, and a test that reached for a real source would
be re-testing the transport instead of the pipeline.
"""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from unifideck.stores.gamevault.archive import (
    available_extractors,
    detect_format,
    extract_archive,
)
from unifideck.stores.gamevault.filename import ARCHIVE_EXTENSIONS
from unifideck.stores.gamevault.install import GameVaultInstaller
from unifideck.stores.gamevault.markers import (
    load_install_info,
    remove_install_info,
    save_install_info,
)
from unifideck.stores.gamevault.sources import AcquiredArchive


class _FakeSource:
    """An ``ArchiveSource`` over a file that is already on disk."""

    def __init__(
        self,
        path: Path,
        *,
        title: str = "My Game",
        dir_name: str = "My Game",
        prefer_native: bool = False,
        is_installer: bool = False,
        error: Exception | None = None,
    ) -> None:
        self._acquired = AcquiredArchive(
            path=path,
            title=title,
            dir_name=dir_name,
            prefer_native=prefer_native,
            is_installer=is_installer,
        )
        self._error = error
        self.released: list[AcquiredArchive | None] = []

    async def acquire(self, game_id, *, progress_callback):
        if self._error is not None:
            raise self._error
        return self._acquired

    def release(self, acquired):
        self.released.append(acquired)

    async def size(self, game_id):
        return 4242


def _zip_with(tmp_path: Path, name: str, members: dict[str, bytes]) -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return archive


@pytest.fixture
def markers_in(tmp_path, monkeypatch):
    """Redirect the marker directory at its single binding."""
    import unifideck.stores.gamevault.markers as markers_mod
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    monkeypatch.setattr(markers_mod, "_marker_dir", lambda: marker_dir)
    return marker_dir


# ── detect_format ────────────────────────────────────────────────────
def test_detect_format_zip(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    assert detect_format(p) == "zip"


def test_detect_format_rar(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 20)
    assert detect_format(p) == "rar"


def test_detect_format_7z(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"7z\xbc\xaf'\x1c" + b"\x00" * 20)
    assert detect_format(p) == "7z"


def test_detect_format_7z_inside_sfx(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"MZ" + b"\x00" * 2048 + b"7z\xbc\xaf'\x1c" + b"\x00" * 20)
    assert detect_format(p) == "7z"


@pytest.mark.parametrize(
    ("label", "blob"),
    [
        ("gzip", b"\x1f\x8b\x08" + b"\x00" * 32),
        ("bzip2", b"BZh9" + b"\x00" * 32),
        ("xz", b"\xfd7zXZ\x00\x00" + b"\x00" * 32),
        ("zstd", b"\x28\xb5\x2f\xfd" + b"\x00" * 32),
        ("cab", b"MSCF" + b"\x00" * 32),
        ("wim", b"MSWIM\x00\x00\x00" + b"\x00" * 32),
        ("tar", b"\x00" * 257 + b"ustar\x0000" + b"\x00" * 32),
        ("iso", b"\x00" * 0x8001 + b"CD001" + b"\x00" * 32),
    ],
)
def test_detect_format_recognises_every_libarchive_format(tmp_path, label, blob):
    """The indexer offers these, so detection has to answer for them.

    ``ARCHIVE_EXTENSIONS`` has always listed tar, gzip, iso, wim and cab,
    which meant a shortcut in the user's library. ``detect_format`` knew only
    zip/rar/7z, so every one of those installs ended at "Unknown archive
    format" — a promise the library made and the installer could not keep.
    """
    p = tmp_path / f"archive_{label}.bin"
    p.write_bytes(blob)
    assert detect_format(p) == "libarchive"


def test_every_indexed_extension_is_a_format_the_installer_knows():
    """No entry in ``ARCHIVE_EXTENSIONS`` may be unknown to ``detect_format``.

    The two lists drifted apart once already. This is the assertion that ties
    them together: a new extension added to the indexer without a matching
    magic-byte branch fails here rather than on a user's device after a
    multi-gigabyte download.
    """
    samples = {
        ".zip": b"PK\x03\x04",
        ".7z": b"7z\xbc\xaf'\x1c",
        ".rar": b"Rar!\x1a\x07\x00",
        ".tar": b"\x00" * 257 + b"ustar\x0000",
        ".tar.gz": b"\x1f\x8b\x08",
        ".tar.bz2": b"BZh9",
        ".tar.xz": b"\xfd7zXZ\x00\x00",
        ".tar.zst": b"\x28\xb5\x2f\xfd",
        ".iso": b"\x00" * 0x8001 + b"CD001",
        ".wim": b"MSWIM\x00\x00\x00",
        ".cab": b"MSCF",
    }
    assert set(samples) == set(ARCHIVE_EXTENSIONS)


def test_detect_format_unknown(tmp_path):
    p = tmp_path / "archive.bin"
    p.write_bytes(b"NOPE" + b"\x00" * 20)
    assert detect_format(p) is None


def test_detect_format_short_file_is_not_mistaken_for_tar(tmp_path):
    """A file too small to hold the offset magic must not read past its end."""
    p = tmp_path / "tiny.bin"
    p.write_bytes(b"AB")
    assert detect_format(p) is None


def test_detect_format_missing_file_returns_none(tmp_path):
    assert detect_format(tmp_path / "nope.bin") is None


# ── the extractor ladder ─────────────────────────────────────────────
def test_bsdtar_is_first_in_the_available_ladder():
    """``bsdtar`` must be preferred wherever it exists.

    Stock SteamOS ships ``bsdtar`` and does not ship ``7z``. The two used to
    be separate ladders, and the 7z one required the ``7z`` binary outright,
    so a ``.7z`` upload failed on an untouched Deck after the user had
    already waited out a multi-gigabyte download.
    """
    available = available_extractors()
    if "bsdtar" in available:
        assert available[0] == "bsdtar"


async def test_extract_zip_uses_the_stdlib_and_needs_no_tool(tmp_path):
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 100})
    dest = tmp_path / "out"

    await extract_archive(archive, dest)

    assert (dest / "Game.exe").read_bytes() == b"x" * 100


async def test_extract_unknown_format_raises(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"NOPE" + b"\x00" * 32)

    with pytest.raises(RuntimeError, match="Unknown archive format"):
        await extract_archive(junk, tmp_path / "out")


# ── the shared install pipeline ──────────────────────────────────────
async def test_install_extracts_and_registers(tmp_path, markers_in):
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 2000})
    source = _FakeSource(archive)
    installer = GameVaultInstaller(
        source=source, default_install_root=str(tmp_path / "installs"),
    )

    result = await installer.install_game("g1")

    assert result.success is True
    game_dir = tmp_path / "installs" / "My Game"
    assert (game_dir / "Game.exe").exists()
    assert result.install_path == str(game_dir)
    assert result.metadata["exe_path"] == str(game_dir / "Game.exe")

    marker = load_install_info("g1")
    assert marker["install_path"] == str(game_dir)
    assert marker["archive_path"] == str(archive)


async def test_install_reports_progress_phases(tmp_path, markers_in):
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 10})
    seen: list[str] = []

    async def progress(payload):
        seen.append(payload.get("phase", ""))

    installer = GameVaultInstaller(
        source=_FakeSource(archive),
        default_install_root=str(tmp_path / "installs"),
    )
    await installer.install_game("g1", progress_callback=progress)

    assert "extracting" in seen


async def test_install_releases_the_source_on_success(tmp_path, markers_in):
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x"})
    source = _FakeSource(archive)
    installer = GameVaultInstaller(
        source=source, default_install_root=str(tmp_path / "installs"),
    )

    await installer.install_game("g1")

    assert len(source.released) == 1


async def test_install_releases_the_source_on_failure(tmp_path, markers_in):
    """``release`` runs from a ``finally``, so a failure still hands back.

    Without it, remote mode would leak a multi-gigabyte staged download for
    every failed install.
    """
    source = _FakeSource(tmp_path / "nope.zip", error=RuntimeError("boom"))
    installer = GameVaultInstaller(
        source=source, default_install_root=str(tmp_path / "installs"),
    )

    result = await installer.install_game("g1")

    assert result.success is False
    assert result.error == "boom"
    assert source.released == [None]


async def test_install_passes_the_native_hint_to_the_exe_finder(
    tmp_path, markers_in,
):
    """A ``(L_P)`` archive resolves its native binary, not the bundled exe."""
    archive = _zip_with(
        tmp_path,
        "g.zip",
        {"Game.exe": b"x" * 5_000_000, "start.sh": b"#!/bin/sh\n"},
    )
    installer = GameVaultInstaller(
        source=_FakeSource(archive, prefer_native=True),
        default_install_root=str(tmp_path / "installs"),
    )

    result = await installer.install_game("g1")

    assert result.metadata["exe_path"].endswith("start.sh")


async def test_install_surfaces_the_installer_flag(tmp_path, markers_in):
    archive = _zip_with(tmp_path, "g.zip", {"Setup.exe": b"x" * 100})
    installer = GameVaultInstaller(
        source=_FakeSource(archive, is_installer=True),
        default_install_root=str(tmp_path / "installs"),
    )

    result = await installer.install_game("g1")

    assert result.metadata["is_installer"] is True


async def test_install_honours_an_explicit_install_path(tmp_path, markers_in):
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x"})
    elsewhere = tmp_path / "sdcard"
    installer = GameVaultInstaller(
        source=_FakeSource(archive),
        default_install_root=str(tmp_path / "installs"),
    )

    result = await installer.install_game("g1", install_path=str(elsewhere))

    assert result.install_path == str(elsewhere / "My Game")


async def test_get_game_size_comes_from_the_source(tmp_path):
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root=str(tmp_path),
    )
    assert await installer.get_game_size("g1") == 4242


# ── uninstall ────────────────────────────────────────────────────────
async def test_uninstall_game_missing_install_returns_failure(tmp_path, markers_in):
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root=str(tmp_path),
    )

    result = await installer.uninstall_game("never-installed")

    assert result.success is False
    assert result.error == "Game not installed"


async def test_uninstall_game_removes_dir_and_marker(tmp_path, markers_in):
    game_dir = tmp_path / "installs" / "installed_game"
    game_dir.mkdir(parents=True)
    (game_dir / "Game.exe").write_bytes(b"x")
    save_install_info(
        "55",
        title="T",
        install_path=str(game_dir),
        exe_path=str(game_dir / "Game.exe"),
    )
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root=str(tmp_path / "installs"),
    )

    result = await installer.uninstall_game("55")

    assert result.success is True
    assert not game_dir.exists()
    assert load_install_info("55") is None


async def test_uninstall_removes_the_install_dir_and_leaves_the_vault_alone(
    tmp_path, markers_in,
):
    """The archive survives because it is outside the directory being removed.

    That is structural, not a check: ``install_path`` is always
    ``<install root>/<game dir>`` and the extraction created that directory,
    so a vault archive is never inside it.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    archive = _zip_with(vault, "My Game (2016).zip", {"Game.exe": b"x" * 100})
    installer = GameVaultInstaller(
        source=_FakeSource(archive),
        default_install_root=str(tmp_path / "installs"),
    )

    installed = await installer.install_game("g1")
    game_dir = Path(installed.install_path)
    assert (game_dir / "Game.exe").exists()

    result = await installer.uninstall_game("g1")

    assert result.success is True
    assert not game_dir.exists()
    assert archive.exists()
    assert load_install_info("g1") is None


async def test_uninstall_never_touches_the_source_archive(tmp_path, markers_in):
    """The local-mode promise, asserted end to end.

    Install from an archive the user owns, then uninstall: the extracted
    tree goes and the archive stays. It is the single behaviour a user would
    never forgive getting wrong, so it is pinned end to end as well as at
    the guard.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    archive = _zip_with(vault, "My Game (2016).zip", {"Game.exe": b"x" * 100})
    installer = GameVaultInstaller(
        source=_FakeSource(archive),
        default_install_root=str(tmp_path / "installs"),
    )

    await installer.install_game("g1")
    game_dir = tmp_path / "installs" / "My Game"
    assert game_dir.exists()

    result = await installer.uninstall_game("g1")

    assert result.success is True
    assert not game_dir.exists()
    assert archive.exists()


# ── install marker persistence ───────────────────────────────────────
def test_save_and_load_install_info_roundtrip(markers_in):
    save_install_info(
        "123",
        title="My Game",
        install_path="/games/mygame",
        exe_path="/games/mygame/Game.exe",
        archive_path="/vault/My Game.zip",
    )
    assert load_install_info("123") == {
        "game_id": "123",
        "title": "My Game",
        "install_path": "/games/mygame",
        "exe_path": "/games/mygame/Game.exe",
        "archive_path": "/vault/My Game.zip",
    }


def test_save_install_info_archive_path_defaults_to_empty(markers_in):
    save_install_info("1", title="T", install_path="/p", exe_path="/p/e.exe")
    assert load_install_info("1")["archive_path"] == ""


def test_load_install_info_missing_returns_none(markers_in):
    assert load_install_info("nope") is None


def test_load_install_info_corrupt_json_returns_none(markers_in):
    (markers_in / "bad.json").write_text("{not json")
    assert load_install_info("bad") is None


def test_remove_install_info_deletes_marker(markers_in):
    save_install_info("9", title="T", install_path="/p", exe_path="")
    remove_install_info("9")
    assert load_install_info("9") is None


def test_remove_install_info_missing_is_a_noop(markers_in):
    remove_install_info("absent")


def test_get_installed_reads_all_markers(tmp_path, markers_in):
    save_install_info("1", title="A", install_path="/a", exe_path="")
    save_install_info("2", title="B", install_path="/b", exe_path="")
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root=str(tmp_path),
    )
    assert set(installer.get_installed()) == {"1", "2"}


def test_get_installed_empty_dir_returns_empty(tmp_path, markers_in):
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root=str(tmp_path),
    )
    assert installer.get_installed() == {}


def test_get_install_info_delegates_to_module_function(tmp_path, markers_in):
    save_install_info("7", title="T", install_path="/p", exe_path="/p/e.exe")
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root=str(tmp_path),
    )
    assert installer.get_install_info("7")["title"] == "T"


# ── construction ─────────────────────────────────────────────────────
def test_installer_expands_user_paths(tmp_path):
    installer = GameVaultInstaller(
        source=_FakeSource(tmp_path / "x.zip"),
        default_install_root="~/Games/GameVault",
    )
    assert "~" not in str(installer._default_install_root)


# ── install-directory ownership ──────────────────────────────────────
# ``<install_root>/<title>`` is the folder name every store derives, so two
# stores routinely pick the same one. That is not survivable: GOG's install
# planner deletes unrecognised data in its target, which on a real device
# destroyed a GameVault extraction of Bastion four times in one evening.
@pytest.fixture
def games_map_at(tmp_path, monkeypatch):
    """Point ``foreign_installs_under`` at a games.map this test controls.

    Patched at ``unifideck.utils.paths``, not on ``safe_delete``: the helper
    imports ``get_games_map_path`` inside the function body, so the module
    attribute is what it actually resolves at call time.
    """
    path = tmp_path / "games.map"
    path.write_text("")
    monkeypatch.setattr(
        "unifideck.utils.paths.get_games_map_path", lambda config=None: str(path),
    )
    return path


async def test_install_avoids_a_directory_another_store_owns(
    tmp_path, markers_in, games_map_at,
):
    root = tmp_path / "installs"
    taken = root / "My Game"
    taken.mkdir(parents=True)
    games_map_at.write_text(
        f"gog:1423058311={taken}/game/Game.exe\t{taken}\t-123\n",
    )
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 2000})
    installer = GameVaultInstaller(
        source=_FakeSource(archive), default_install_root=str(root),
    )

    result = await installer.install_game("g1")

    assert result.success is True
    assert result.install_path == str(root / "My Game (GameVault)")
    assert (root / "My Game (GameVault)" / "Game.exe").exists()
    # GOG's directory was left exactly as it was.
    assert list(taken.iterdir()) == []
    assert load_install_info("g1")["install_path"] == str(
        root / "My Game (GameVault)",
    )


async def test_install_reuses_its_own_directory_on_reinstall(
    tmp_path, markers_in, games_map_at,
):
    """A row this game owns is not foreign — reinstall must not drift."""
    root = tmp_path / "installs"
    mine = root / "My Game"
    mine.mkdir(parents=True)
    games_map_at.write_text(
        f"gamevault:g1={mine}/Game.exe\t{mine}\t-123\n",
    )
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 2000})
    installer = GameVaultInstaller(
        source=_FakeSource(archive), default_install_root=str(root),
    )

    result = await installer.install_game("g1")

    assert result.install_path == str(mine)


async def test_install_ignores_an_unrelated_row_elsewhere(
    tmp_path, markers_in, games_map_at,
):
    """Ownership is by path containment, not by name similarity."""
    root = tmp_path / "installs"
    other = tmp_path / "somewhere else" / "My Game"
    other.mkdir(parents=True)
    games_map_at.write_text(f"gog:99={other}/Game.exe\t{other}\t-1\n")
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 2000})
    installer = GameVaultInstaller(
        source=_FakeSource(archive), default_install_root=str(root),
    )

    result = await installer.install_game("g1")

    assert result.install_path == str(root / "My Game")


# ── cancellation ─────────────────────────────────────────────────────
# The store had no cancel handling at all: the frontend's Cancel button is
# not store-gated, so it reached ``DownloadService.cancel`` → ``task.cancel``
# and reported success while the download, the extractor process and the
# half-written install directory all carried on or were left behind.
class _SlowSource(_FakeSource):
    """A source whose ``acquire`` never finishes on its own."""

    def __init__(self, path: Path, started: asyncio.Event) -> None:
        super().__init__(path)
        self._started = started

    async def acquire(self, game_id, *, progress_callback):
        self._started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


async def test_cancel_during_acquire_propagates_and_releases(
    tmp_path, markers_in,
):
    """Cancellation must not be swallowed by the pipeline's ``except``.

    ``CancelledError`` is a ``BaseException``, so the broad ``except
    Exception`` correctly lets it past — if that ever changed, the worker
    would record a failed install instead of a cancelled one.
    """
    started = asyncio.Event()
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 10})
    source = _SlowSource(archive, started)
    installer = GameVaultInstaller(
        source=source, default_install_root=str(tmp_path / "installs"),
    )

    task = asyncio.ensure_future(installer.install_game("g1"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert source.released == [None]
    assert load_install_info("g1") is None


async def test_cancel_removes_the_half_extracted_directory(
    tmp_path, markers_in, games_map_at, monkeypatch,
):
    """No marker is written until success, so nothing else could reclaim it.

    ``uninstall_game`` answers "Game not installed" for a directory with no
    marker, which left however many GB had landed with no route out of the
    UI.
    """
    root = tmp_path / "installs"
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 10})
    reached = asyncio.Event()

    async def _hang(_archive, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial.bin").write_bytes(b"x" * 100)
        reached.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "unifideck.stores.gamevault.install.extract_archive", _hang,
    )
    installer = GameVaultInstaller(
        source=_FakeSource(archive), default_install_root=str(root),
    )

    task = asyncio.ensure_future(installer.install_game("g1"))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (root / "My Game").exists()


async def test_cancel_keeps_a_directory_that_already_existed(
    tmp_path, markers_in, games_map_at, monkeypatch,
):
    """Only a directory this run created may be deleted.

    Extracting into a folder that was already there and then removing it on
    cancel would take whatever else the user kept in it.
    """
    root = tmp_path / "installs"
    existing = root / "My Game"
    existing.mkdir(parents=True)
    (existing / "keep me.txt").write_text("mine")
    reached = asyncio.Event()

    async def _hang(_archive, dest):
        reached.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "unifideck.stores.gamevault.install.extract_archive", _hang,
    )
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 10})
    installer = GameVaultInstaller(
        source=_FakeSource(archive), default_install_root=str(root),
    )

    task = asyncio.ensure_future(installer.install_game("g1"))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (existing / "keep me.txt").read_text() == "mine"


async def test_cancel_leaves_a_directory_another_store_has_claimed(
    tmp_path, markers_in, games_map_at, monkeypatch,
):
    """Ownership is re-checked at delete time, not trusted from before.

    An extract can run for a long time, and the folder may have been claimed
    while it did.
    """
    root = tmp_path / "installs"
    game_dir = root / "My Game"
    reached = asyncio.Event()

    async def _hang(_archive, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial.bin").write_bytes(b"x")
        games_map_at.write_text(
            f"gog:99={game_dir}/Game.exe\t{game_dir}\t-1\n",
        )
        reached.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "unifideck.stores.gamevault.install.extract_archive", _hang,
    )
    archive = _zip_with(tmp_path, "g.zip", {"Game.exe": b"x" * 10})
    installer = GameVaultInstaller(
        source=_FakeSource(archive), default_install_root=str(root),
    )

    task = asyncio.ensure_future(installer.install_game("g1"))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (game_dir / "partial.bin").exists()


async def test_zip_extraction_really_stops_when_cancelled(tmp_path):
    """The extract thread must stop, not just stop being awaited.

    Cancelling ``await asyncio.to_thread(extractall)`` looks instant — the
    wrapped future cancels on the asyncio side straight away — while the
    worker thread carries on writing the entire archive into a directory the
    caller has already given up on. Measured on the pre-fix code: 1 file on
    disk when cancel returned, all 200 two seconds later.

    So the assertion has to be made *after* a pause. Counting at the moment
    of cancellation passes either way and tests nothing.
    """
    members = {f"file_{i:03d}.bin": b"x" * 200_000 for i in range(200)}
    archive = _zip_with(tmp_path, "big.zip", members)
    dest = tmp_path / "out"

    task = asyncio.ensure_future(extract_archive(archive, dest))
    while not (dest.exists() and any(dest.iterdir())):
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    settled = len(list(dest.iterdir()))

    await asyncio.sleep(2.0)

    assert len(list(dest.iterdir())) == settled, (
        "extraction kept running after cancellation"
    )
    assert settled < len(members)
