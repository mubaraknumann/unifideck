"""Tests for ``stores.gamevault.exe_finder`` — picking the launch target.

Moved out of ``test_gamevault_install.py`` with the code, and extended for
native Linux builds, which local mode makes common: a vault folder on a Steam
Deck is routinely half native, and an ``.exe``-only scorer answered those with
``None`` — an install that reports success and can never launch.
"""
from __future__ import annotations

import os
import stat

from unifideck.stores.gamevault.exe_finder import find_executable


def _elf(path, size: int = 4096) -> None:
    """Write a file that looks and behaves like a native Linux binary."""
    path.write_bytes(b"\x7fELF" + b"\x00" * size)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# ── Windows executables ──────────────────────────────────────────────
def test_find_executable_picks_the_only_exe(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_filters_utility_keywords(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    (tmp_path / "unins000.exe").write_bytes(b"x" * 1000)
    (tmp_path / "vcredist_x64.exe").write_bytes(b"x" * 1000)
    (tmp_path / "UE4PrereqSetup_x64.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_no_candidates_returns_none(tmp_path):
    (tmp_path / "readme.txt").write_text("hi")
    assert find_executable(str(tmp_path)) is None


def test_find_executable_prefers_shallower_path(tmp_path):
    """Two otherwise-equal exes: the shallower one wins on depth score.

    The nested path deliberately avoids ``_PRUNE_DIRS`` names, or the walk
    would skip it and the test would pass without exercising the scoring it
    claims to.
    """
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    nested = tmp_path / "engine" / "bin" / "tools"
    nested.mkdir(parents=True)
    (nested / "Other.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_find_executable_prefers_larger_file_at_equal_depth(tmp_path):
    (tmp_path / "Small.exe").write_bytes(b"x" * 1000)
    (tmp_path / "Big.exe").write_bytes(b"x" * (10 * 1024 * 1024))
    assert find_executable(str(tmp_path)) == str(tmp_path / "Big.exe")


def test_find_executable_does_not_filter_trainer_or_cheat_by_name(tmp_path):
    """Regression documentation, not a desired behaviour: KNOWN GAP.

    ``_UTIL_KEYWORDS`` has no "trainer"/"cheat" entry, so a large trainer
    exe bundled inside a game's install dir can outscore (and thus replace)
    the real game exe purely on file size. This test pins the CURRENT
    behaviour so a future fix has to consciously change it rather than
    silently regress.
    """
    (tmp_path / "Game.exe").write_bytes(b"x" * (1 * 1024 * 1024))
    (tmp_path / "trainer.exe").write_bytes(b"x" * (20 * 1024 * 1024))
    assert find_executable(str(tmp_path)) == str(tmp_path / "trainer.exe")


def test_prune_dirs_keeps_redist_binaries_out_of_the_running(tmp_path):
    """A redist folder is skipped wholesale, not merely demoted."""
    redist = tmp_path / "_CommonRedist" / "vcredist"
    redist.mkdir(parents=True)
    (redist / "Huge.exe").write_bytes(b"x" * (50 * 1024 * 1024))
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


# ── Degrade, don't eliminate ─────────────────────────────────────────
#
# The keyword filter expresses a preference. When it rejects every
# candidate, returning None produced an install that reported success and
# could never launch: the first real GameVault install extracted a repack
# whose only executable was ``Setup.exe``, the filter rejected it on
# "setup", the marker got ``exe_path: ""`` and reconcile logged
#
#   mark_installed gamevault:1 — empty exe_path; launcher will not be able
#   to resolve a target
def test_find_executable_falls_back_when_everything_is_filtered(tmp_path):
    """A repack whose only executable is its installer."""
    repack = tmp_path / "Ghost of Tsushima [DODI Repack]"
    repack.mkdir()
    (repack / "Setup.exe").write_bytes(b"x" * 8_000_000)

    assert find_executable(str(tmp_path)) == str(repack / "Setup.exe")


def test_find_executable_still_prefers_a_real_game_exe(tmp_path):
    """The fallback must not weaken the preference when both exist."""
    (tmp_path / "Setup.exe").write_bytes(b"x" * 9_000_000)
    (tmp_path / "GhostOfTsushima.exe").write_bytes(b"x" * 1000)

    assert find_executable(str(tmp_path)) == str(tmp_path / "GhostOfTsushima.exe")


def test_find_executable_returns_none_when_there_is_no_exe(tmp_path):
    (tmp_path / "readme.txt").write_text("no executables here")

    assert find_executable(str(tmp_path)) is None


# ── Native Linux builds ──────────────────────────────────────────────
def test_finds_a_shell_launcher(tmp_path):
    (tmp_path / "start.sh").write_text("#!/bin/sh\nexec ./game\n")
    assert find_executable(str(tmp_path)) == str(tmp_path / "start.sh")


def test_finds_an_appimage(tmp_path):
    (tmp_path / "Celeste.AppImage").write_bytes(b"x" * 5000)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Celeste.AppImage")


def test_finds_an_executable_elf_with_no_extension(tmp_path):
    _elf(tmp_path / "BabaIsYou")
    assert find_executable(str(tmp_path)) == str(tmp_path / "BabaIsYou")


def test_ignores_an_elf_that_is_not_executable(tmp_path):
    """A shipped ``.so``-alike with no ``+x`` bit is not a launch target."""
    (tmp_path / "libdata").write_bytes(b"\x7fELF" + b"\x00" * 4096)
    os.chmod(tmp_path / "libdata", 0o644)
    assert find_executable(str(tmp_path)) is None


def test_ignores_a_non_elf_extensionless_file(tmp_path):
    blob = tmp_path / "gamedata"
    blob.write_bytes(b"NOTELF" + b"\x00" * 100)
    blob.chmod(0o755)
    assert find_executable(str(tmp_path)) is None


def test_conventional_launcher_beats_a_bare_elf(tmp_path):
    _elf(tmp_path / "game_bin", size=20_000_000)
    (tmp_path / "start.sh").write_text("#!/bin/sh\n")
    assert find_executable(str(tmp_path)) == str(tmp_path / "start.sh")


def test_prefer_native_reorders_a_mixed_archive(tmp_path):
    """An archive labelled ``(L_P)`` that also ships a Windows build."""
    (tmp_path / "Game.exe").write_bytes(b"x" * (50 * 1024 * 1024))
    _elf(tmp_path / "Game")

    assert find_executable(str(tmp_path), prefer_native=True) == str(
        tmp_path / "Game",
    )
    assert find_executable(str(tmp_path), prefer_native=False) == str(
        tmp_path / "Game.exe",
    )


def test_prefer_native_still_falls_back_to_windows_when_mislabelled(tmp_path):
    """A wrong type token must not make the game unlaunchable."""
    (tmp_path / "Game.exe").write_bytes(b"x" * 1000)
    assert find_executable(str(tmp_path), prefer_native=True) == str(
        tmp_path / "Game.exe",
    )


# ── MonoKickstart Linux builds ───────────────────────────────────────
# A GOG Linux game ships a PE32 ``.exe`` that is a Mono assembly, plus a bash
# script named after the game and native ``.so``s in ``lib64``. Proton cannot
# run the assembly; the script is the entry point, and the launcher already
# routes a non-``.exe`` target down its native path. See UD notes for Bastion.
def _mono_linux_build(root, *, title: str = "Bastion"):
    """A GOG-shaped native Linux build under ``root/game``."""
    game = root / "game"
    game.mkdir(parents=True)
    (game / f"{title}.exe").write_bytes(b"MZ" + b"\x00" * (3 * 1024 * 1024))
    launcher = game / title
    launcher.write_text("#!/bin/bash\ncd \"`dirname \"$0\"`\"\n")
    launcher.chmod(0o755)
    (game / "monoconfig").write_text("<configuration/>")
    lib64 = game / "lib64"
    lib64.mkdir()
    (lib64 / "libmono-2.0.so.1").write_bytes(b"\x7fELF" + b"\x00" * 100)
    return game


def test_shebang_script_is_a_native_candidate(tmp_path):
    """An extensionless bash script is launchable; the ELF-only probe missed it."""
    script = tmp_path / "Bastion"
    script.write_text("#!/bin/bash\necho hi\n")
    script.chmod(0o755)
    assert find_executable(str(tmp_path)) == str(script)


def test_mono_linux_build_picks_the_script_not_the_exe(tmp_path):
    """The whole Bastion defect, end to end: no ``prefer_native`` token needed."""
    game = _mono_linux_build(tmp_path)
    assert find_executable(str(tmp_path), title="Bastion") == str(
        game / "Bastion",
    )


def test_mono_linux_build_is_fixed_without_a_title(tmp_path):
    """The demotion alone is enough — ``title`` only sharpens the score."""
    game = _mono_linux_build(tmp_path)
    assert find_executable(str(tmp_path)) == str(game / "Bastion")


def test_mono_marker_via_lib64_only(tmp_path):
    """``libmono-*.so`` in a pruned lib dir is detected by name, not by walking."""
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(b"MZ" + b"\x00" * 1000)
    script = game / "Game"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    lib64 = game / "lib64"
    lib64.mkdir()
    (lib64 / "libmono-2.0.so.1").write_bytes(b"\x7fELF")
    assert find_executable(str(tmp_path)) == str(script)


def test_mono_exe_still_wins_when_there_is_no_script(tmp_path):
    """Demotion, not rejection: a lone assembly beats returning nothing."""
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "Game.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 1000)
    (game / "monoconfig").write_text("<configuration/>")
    assert find_executable(str(tmp_path)) == str(exe)


def test_a_plain_windows_game_is_unaffected_by_the_mono_probe(tmp_path):
    """No Mono markers → the ``.exe`` keeps winning over an incidental script."""
    (tmp_path / "Game.exe").write_bytes(b"MZ" + b"\x00" * (5 * 1024 * 1024))
    helper = tmp_path / "runme"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    assert find_executable(str(tmp_path)) == str(tmp_path / "Game.exe")


def test_title_match_ignores_punctuation_and_case(tmp_path):
    """``Baldurs Gate`` the title vs ``BaldursGate`` the file."""
    script = tmp_path / "BaldursGate"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    _elf(tmp_path / "other_bin", size=20_000_000)
    assert find_executable(
        str(tmp_path), title="Baldur's Gate",
    ) == str(script)
