from __future__ import annotations

from pathlib import Path

from backend.utils.vdf import load_shortcuts_vdf, save_shortcuts_vdf


def test_load_shortcuts_missing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.vdf"
    assert load_shortcuts_vdf(str(missing_path)) == {"shortcuts": {}}


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.vdf"
    data = {
        "shortcuts": {
            "0": {
                "appname": "Test Game",
                "exe": "test.exe",
                "StartDir": "",
                "icon": "",
                "ShortcutPath": "",
                "LaunchOptions": "",
                "IsHidden": 0,
                "AllowDesktopConfig": 1,
                "AllowOverlay": 1,
                "OpenVR": 0,
                "Devkit": 0,
                "DevkitGameID": "",
                "DevkitOverrideAppID": 0,
                "LastPlayTime": 0,
                "tags": {},
            }
        }
    }
    assert save_shortcuts_vdf(str(path), data) is True
    loaded = load_shortcuts_vdf(str(path))
    assert "shortcuts" in loaded
    assert len(loaded["shortcuts"]) == len(data["shortcuts"])
