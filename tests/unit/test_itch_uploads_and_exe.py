"""Tests for ``stores.itch.uploads`` and ``stores.itch.exe``.

The layouts are the two real installs from the 2026-09-23 spike: an NW.js
Linux build whose helper binary is bigger than the game binary, and a Ren'Py
"PC" zip (Windows upload) that carries its own Linux runtime.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from unifideck.stores.itch.exe import manifest_play_target, resolve_launch_target, upload_platform
from unifideck.stores.itch.uploads import choose_upload


def _up(uid: int, name: str, platforms: dict[str, str], **kw: Any) -> dict[str, Any]:
    return {"id": uid, "filename": name, "type": kw.get("type", "default"),
            "platforms": platforms, "storage": kw.get("storage", "hosted"),
            "demo": kw.get("demo", False), "size": kw.get("size", 100)}


# ── upload choice ──────────────────────────────────────────────────────
def test_linux_upload_wins_over_windows() -> None:
    uploads = [_up(1, "game-win.zip", {"windows": "all"}), _up(2, "game-linux.zip", {"linux": "all"})]
    assert choose_upload(uploads)["id"] == 2


def test_full_build_beats_a_smaller_demo() -> None:
    # The test account's smallest Windows upload was CroakingAroundDEMO.zip.
    uploads = [_up(1, "CroakingAroundDEMO.zip", {"windows": "all"}, demo=True, size=10),
               _up(2, "CroakingAround.zip", {"windows": "all"}, size=500)]
    assert choose_upload(uploads)["id"] == 2


def test_uploads_butler_cannot_install_are_never_chosen() -> None:
    uploads = [
        _up(1, "soundtrack.zip", {"windows": "all"}, type="soundtrack"),
        _up(2, "index.html", {}, type="html"),
        _up(3, "game.zip", {"windows": "all"}, storage="external"),
        _up(4, "setup.msi", {"windows": "all"}),
        _up(5, "game.deb", {"linux": "all"}),
        _up(6, "game-mac.zip", {"osx": "all"}),
    ]
    assert choose_upload(uploads) is None


def test_archive_and_64_bit_are_preferred() -> None:
    uploads = [_up(1, "game-x86.zip", {"windows": "all"}), _up(2, "game.exe", {"windows": "all"}),
               _up(3, "game-x86_64.zip", {"windows": "all"})]
    assert choose_upload(uploads)["id"] == 3


def test_upload_platform_prefers_linux_when_both() -> None:
    assert upload_platform({"platforms": {"linux": "all", "windows": "all"}}) == "linux"
    assert upload_platform({"platforms": {"windows": "all"}}) == "windows"


# ── launch target ──────────────────────────────────────────────────────
def _elf(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + b"\0" * size)
    os.chmod(path, 0o755)


def test_nwjs_build_launches_nw_not_the_bigger_nacl_helper(tmp_path: Path) -> None:
    root = tmp_path / "fear-assessment"
    _elf(root / "linux64" / "nw", 222_784)
    _elf(root / "linux64" / "nacl_helper", 2_697_864)
    _elf(root / "linux64" / "nacl_helper_bootstrap", 15_880)
    (root / "linux64" / "package.nw").write_bytes(b"\0" * 10)
    assert resolve_launch_target(str(root), "linux", "Fear Assessment") == str(root / "linux64" / "nw")


def _renpy_pc_zip(root: Path, script_name: str) -> None:
    base = root / "HamptonCourt-1.0-pc"
    base.mkdir(parents=True)
    (base / "HamptonCourt.exe").write_bytes(b"MZ" + b"\0" * 1000)
    (base / script_name).write_text("#!/bin/sh\nexec ./lib/linux-x86_64/HamptonCourt\n")
    os.chmod(base / script_name, 0o755)


def test_windows_upload_with_a_game_named_launcher_runs_natively(tmp_path: Path) -> None:
    _renpy_pc_zip(tmp_path, "HamptonCourt.sh")
    target = resolve_launch_target(str(tmp_path), "windows", "Hampton Court")
    assert target is not None and target.endswith("HamptonCourt.sh")


def test_windows_upload_with_a_stray_script_keeps_the_exe(tmp_path: Path) -> None:
    _renpy_pc_zip(tmp_path, "build_tools.sh")
    target = resolve_launch_target(str(tmp_path), "windows", "Hampton Court")
    assert target is not None and target.endswith("HamptonCourt.exe")


def test_itch_toml_play_action_wins(tmp_path: Path) -> None:
    _elf(tmp_path / "bin" / "real-game", 10)
    _elf(tmp_path / "decoy", 5_000_000)
    (tmp_path / ".itch.toml").write_text(
        '[[actions]]\nname = "editor"\npath = "decoy"\n\n'
        '[[actions]]\nname = "play"\npath = "bin/real-game"\nplatform = "linux"\n',
    )
    assert resolve_launch_target(str(tmp_path), "linux", "Game") == str(tmp_path / "bin" / "real-game")


def test_itch_toml_ext_token_url_and_escape_are_handled(tmp_path: Path) -> None:
    (tmp_path / "Game.exe").write_bytes(b"MZ")
    (tmp_path / ".itch.toml").write_text(
        '[[actions]]\nname = "play"\npath = "https://example.com/web"\n\n'
        '[[actions]]\nname = "play"\npath = "../outside{{EXT}}"\n\n'
        '[[actions]]\nname = "play"\npath = "Game{{EXT}}"\n',
    )
    assert manifest_play_target(str(tmp_path), "windows") == str(tmp_path / "Game.exe")


def test_unreadable_manifest_falls_back_to_the_heuristic(tmp_path: Path) -> None:
    _elf(tmp_path / "start.sh", 10)
    (tmp_path / ".itch.toml").write_text("this is [not toml")
    assert resolve_launch_target(str(tmp_path), "linux", "Game") == str(tmp_path / "start.sh")


def test_empty_install_has_no_target(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("hi")
    assert resolve_launch_target(str(tmp_path), "linux", "Game") is None
