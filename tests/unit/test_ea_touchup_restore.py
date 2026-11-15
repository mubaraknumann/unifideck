"""Tests for ``launcher.ea.touchup_restore``: undoing an earlier Touchup's moves."""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.ea.touchup_restore import INSTALL_LOG, restore_moved_files


def _touched_install(tmp_path: Path) -> Path:
    """A build Touchup already ran on: en_US files moved into the game folder, logged lower-cased."""
    install = tmp_path / "game"
    (install / "Data").mkdir(parents=True)
    (install / "Data" / "MinimalRes.syb").write_bytes(b"english")
    (install / "Game.exe").write_bytes(b"exe")
    (install / "__Installer" / "LOC" / "en_US").mkdir(parents=True)
    (install / "__Installer" / "LOC" / "fr_FR" / "Data").mkdir(parents=True)
    (install / "__Installer" / "LOC" / "fr_FR" / "Data" / "MinimalRes.syb").write_bytes(b"french")
    win = "Z:\\home\\deck\\Games\\EA\\game"
    log = "\r\n".join([
        "03:23:35  Install Locale: en_US",
        f"03:24:02  Moving: {win}\\__installer\\loc\\en_us\\data\\minimalres.syb to {win}\\data\\minimalres.syb",
        f"03:24:03  Moving: {win}\\__installer\\loc\\en_us\\game.exe to {win}\\game.exe",
        f"03:24:04  Moving: {win}\\__installer\\loc\\en_us\\video\\gone.bik to {win}\\video\\gone.bik",
        "03:24:32  Installer finished with exit code: 0",
    ])
    (install / INSTALL_LOG).write_bytes(log.encode("utf-16"))
    return install


def test_moved_files_go_back_where_touchup_expects_them(tmp_path: Path) -> None:
    install = _touched_install(tmp_path)

    assert restore_moved_files(install) == 2

    loc = install / "__Installer" / "LOC" / "en_US"
    assert (loc / "Data" / "MinimalRes.syb").read_bytes() == b"english"
    assert (loc / "Game.exe").read_bytes() == b"exe"
    assert not (install / "Data" / "MinimalRes.syb").exists()
    assert not (install / "Game.exe").exists()
    # The other languages are untouched.
    assert (install / "__Installer" / "LOC" / "fr_FR" / "Data" / "MinimalRes.syb").read_bytes() == b"french"


def test_a_link_left_by_an_earlier_restore_becomes_a_move(tmp_path: Path) -> None:
    install = _touched_install(tmp_path)
    loc_exe = install / "__Installer" / "LOC" / "en_US" / "Game.exe"
    loc_exe.hardlink_to(install / "Game.exe")

    restore_moved_files(install)

    assert loc_exe.read_bytes() == b"exe"
    assert not (install / "Game.exe").exists()


def test_a_second_restore_and_a_never_touched_build_change_nothing(tmp_path: Path) -> None:
    install = _touched_install(tmp_path)
    restore_moved_files(install)
    assert restore_moved_files(install) == 0

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert restore_moved_files(fresh) == 0
