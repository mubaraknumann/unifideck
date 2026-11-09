"""Xbox back-compat packages launch the emulator, not the EmuMenu menu.

The menu (``EmuMenu\\EmuMenu.exe``) is a WinUI 3 app that needs the Windows
App Runtime, which no prefix has. ``EmuMenuLaunchArguments.txt`` names the
emulator it would start, and :func:`inspect_game` launches that instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc.game_manifest import inspect_game
from unifideck.launcher.xbox_pc.title_env import title_env

CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <Identity Name="Studio.Classic" Publisher="CN=Studio" Version="1.0.0.0"/>
  <StoreId>C4B8XR1LCXR5</StoreId>
  <ExecutableList><Executable Name="{exe}" Id="App" TargetDeviceFamily="PC"/></ExecutableList>
  <ShellVisuals DefaultDisplayName="Classic Game" PublisherDisplayName="Studio"/>
</Game>
"""


def _package(root: Path, *, exe: str = "EmuMenu\\EmuMenu.exe", args: str | None = "exe=..\\Emu.exe\n",
             emulator: str | None = "Emu.exe") -> Path:
    root.mkdir(exist_ok=True)
    (root / "MicrosoftGame.config").write_text(CONFIG.format(exe=exe))
    menu = root / "EmuMenu"
    menu.mkdir()
    (menu / "EmuMenu.exe").write_bytes(b"\x8e\x11encrypted")
    if args is not None:
        (menu / "EmuMenuLaunchArguments.txt").write_text(f"titlename=Classic Game\n{args}background=..\\bg.png\n")
    if emulator is not None:
        (root / emulator).write_bytes(b"\xe0\x4bencrypted")
    return root


def test_the_menu_is_replaced_by_the_emulator_it_names(tmp_path: Path) -> None:
    game = inspect_game(_package(tmp_path))
    assert game.executable == "Emu.exe"
    assert game.exe_name == "Emu.exe"


def test_the_args_file_and_target_match_in_any_casing(tmp_path: Path) -> None:
    root = _package(tmp_path, exe="emumenu\\EMUMENU.EXE", args=None, emulator="EMU.EXE")
    (root / "EmuMenu").rename(root / "emumenu")
    (root / "emumenu" / "emumenulauncharguments.txt").write_text("EXE = ..\\emu.exe\n")
    assert inspect_game(root).executable == "emu.exe"


@pytest.mark.parametrize("args", [
    None,                       # no arguments file
    "titlename=Classic Game\n",  # no exe= line
    "exe=..\\..\\Emu.exe\n",     # leaves the package
    "exe=C:\\Windows\\Emu.exe\n",  # absolute
    "exe=\\Emu.exe\n",           # rooted
    "exe=..\\Missing.exe\n",     # not in the package
])
def test_anything_unclear_keeps_the_manifest_executable(tmp_path: Path, args: str | None) -> None:
    assert inspect_game(_package(tmp_path, args=args)).executable == "EmuMenu\\EmuMenu.exe"


def test_a_folder_named_like_the_target_is_not_an_executable(tmp_path: Path) -> None:
    root = _package(tmp_path, emulator=None)
    (root / "Emu.exe").mkdir()
    assert inspect_game(root).executable == "EmuMenu\\EmuMenu.exe"


def test_a_game_without_the_menu_is_untouched(tmp_path: Path) -> None:
    root = _package(tmp_path, exe="Game\\Game.exe")
    assert inspect_game(root).executable == "Game\\Game.exe"


def test_only_a_redirected_package_is_flagged_back_compat(tmp_path: Path) -> None:
    assert inspect_game(_package(tmp_path / "a")).backcompat
    assert not inspect_game(_package(tmp_path / "b", args=None)).backcompat
    assert not inspect_game(_package(tmp_path / "c", exe="Game\\Game.exe")).backcompat


def test_back_compat_titles_get_gameinput_v3_and_others_do_not() -> None:
    assert title_env("C4B8XR1LCXR5", backcompat=True) == {"XODUS_GAMEINPUT_V3": "1"}
    assert title_env("C4B8XR1LCXR5") == {}
    assert title_env(None, backcompat=True) == {"XODUS_GAMEINPUT_V3": "1"}
