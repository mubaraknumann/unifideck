"""Unit tests for compat/save_migration — saves survive a prefix rebuild.

Two sources, both merged non-destructively (mtime-guarded, so a save written
after a reset is never clobbered and a repeat run is harmless):

* ``.save_backup`` — what ``prefix_init._reset_prefix`` set aside before wiping;
* the legacy shared umu prefix (``~/Games/umu/umu-0``) that pre-0.6 launches
  wrote into, because they set no per-game ``WINEPREFIX``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from unifideck.launcher.proton.compat import save_migration as sm


def _plan_with_env(prefix_root: Path, gameid: str | None = None):
    """Minimal ProtonLaunchPlan stand-in — only ``env`` is read here."""
    return SimpleNamespace(
        prefix_path=prefix_root,
        env={"GAMEID": gameid} if gameid else {},
        state=SimpleNamespace(proton_tool_id="GE-Proton10-34"),
        context=SimpleNamespace(game_key="gog:123", store="gog"),
    )


def _users_dir_with(root: Path, *, name: str, content: str) -> Path:
    """Build ``root/drive_c/users/steamuser/<name>`` and a user.reg."""
    users = root / "drive_c" / "users" / "steamuser"
    users.mkdir(parents=True, exist_ok=True)
    (root / "user.reg").write_text("reg")
    (users / name).write_text(content)
    return root / "drive_c" / "users"


def test_merge_users_copies_missing(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "steamuser").mkdir(parents=True)
    (src / "steamuser" / "save.dat").write_text("save")

    copied = sm._merge_users(src, dst)

    assert copied == 1
    assert (dst / "steamuser" / "save.dat").read_text() == "save"


def test_merge_users_skips_older_keeps_newer(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "steamuser").mkdir(parents=True)
    (dst / "steamuser").mkdir(parents=True)
    src_file = src / "steamuser" / "save.dat"
    dst_file = dst / "steamuser" / "save.dat"
    src_file.write_text("OLD")
    dst_file.write_text("NEW")
    # Destination is strictly newer than the source.
    import os
    os.utime(src_file, (1000, 1000))
    os.utime(dst_file, (2000, 2000))

    copied = sm._merge_users(src, dst)

    assert copied == 0
    assert dst_file.read_text() == "NEW"  # newer save not clobbered


def test_restore_save_backup_merges_into_users(tmp_path):
    root = tmp_path / "prefix"
    # Live (recreated) prefix has an empty users tree.
    _users_dir_with(root, name=".keep", content="")
    # Backup from a prior reset holds the real save.
    backup = root / ".save_backup" / "steamuser"
    backup.mkdir(parents=True)
    (backup / "save.dat").write_text("savegame")

    sm._restore_save_backup(root)

    restored = root / "drive_c" / "users" / "steamuser" / "save.dat"
    assert restored.read_text() == "savegame"


def test_migrate_legacy_prefix_copies_and_marks(tmp_path, monkeypatch):
    legacy_base = tmp_path / "Games" / "umu"
    monkeypatch.setattr(sm, "_LEGACY_UMU_BASE", str(legacy_base))
    # Legacy shared prefix with a save.
    _users_dir_with(legacy_base / "umu-0", name="save.dat", content="oldsave")
    # Fresh per-game prefix (created but empty users).
    root = tmp_path / "prefix"
    _users_dir_with(root, name=".keep", content="")

    sm._migrate_legacy_prefix(_plan_with_env(root), root)

    migrated = root / "drive_c" / "users" / "steamuser" / "save.dat"
    assert migrated.read_text() == "oldsave"
    assert (root / sm._LEGACY_MIGRATED_MARKER).is_file()


def test_migrate_legacy_prefix_is_idempotent(tmp_path, monkeypatch):
    legacy_base = tmp_path / "Games" / "umu"
    monkeypatch.setattr(sm, "_LEGACY_UMU_BASE", str(legacy_base))
    _users_dir_with(legacy_base / "umu-0", name="save.dat", content="oldsave")
    root = tmp_path / "prefix"
    _users_dir_with(root, name=".keep", content="")
    (root / sm._LEGACY_MIGRATED_MARKER).write_text("done")

    # Marker present → no copy attempted.
    sm._migrate_legacy_prefix(_plan_with_env(root), root)

    assert not (root / "drive_c" / "users" / "steamuser" / "save.dat").exists()


def test_migrate_legacy_prefix_marks_done_when_nothing_found(tmp_path, monkeypatch):
    legacy_base = tmp_path / "Games" / "umu"
    monkeypatch.setattr(sm, "_LEGACY_UMU_BASE", str(legacy_base))
    root = tmp_path / "prefix"
    _users_dir_with(root, name=".keep", content="")

    sm._migrate_legacy_prefix(_plan_with_env(root), root)

    # No legacy data, but the marker is written so we don't rescan.
    assert (root / sm._LEGACY_MIGRATED_MARKER).is_file()


async def test_restore_or_migrate_prefers_save_backup(tmp_path, monkeypatch):
    root = tmp_path / "prefix"
    _users_dir_with(root, name=".keep", content="")
    (root / ".save_backup").mkdir()
    restore = MagicMock()
    migrate = MagicMock()
    monkeypatch.setattr(sm, "_restore_save_backup", restore)
    monkeypatch.setattr(sm, "_migrate_legacy_prefix", migrate)

    await sm.restore_or_migrate_saves(_plan_with_env(root), root)

    restore.assert_called_once()
    migrate.assert_not_called()


async def test_restore_or_migrate_falls_back_to_legacy(tmp_path, monkeypatch):
    root = tmp_path / "prefix"
    _users_dir_with(root, name=".keep", content="")
    restore = MagicMock()
    migrate = MagicMock()
    monkeypatch.setattr(sm, "_restore_save_backup", restore)
    monkeypatch.setattr(sm, "_migrate_legacy_prefix", migrate)

    await sm.restore_or_migrate_saves(_plan_with_env(root), root)

    migrate.assert_called_once()
    restore.assert_not_called()


# ── Regression: a users/ tree is a Windows profile, not a save folder ──
#
# The unfiltered merge copied EVERYTHING under ``drive_c/users``. On the
# machine this was found on that meant a 939 MB CD Projekt Red REDlauncher
# installation out of ``~/Games/umu/umu-0`` was cloned into every new prefix,
# forever — one game's vendor launcher in every other game's prefix. Measured
# on-device: 1097 files / 947.7 MiB skipped, 62 real saves still migrated.


@pytest.mark.parametrize(
    ("relpath", "migratable"),
    [
        # Not save data — these are what bloated every prefix.
        ("steamuser/AppData/Local/Programs/CD Projekt Red/RED.exe", False),
        ("steamuser/AppData/Local/Temp/dd_vcredist_amd64.log", False),
        ("steamuser/AppData/Local/CrashDumps/a.dmp", False),
        ("steamuser/AppData/Local/Microsoft/foo.dat", False),
        ("steamuser/AppData/Local/Packages/x/y", False),
        ("steamuser/AppData/Roaming/Microsoft/x.bin", False),
        # Real saves — must survive. Games legitimately use AppData/Local,
        # which is why the rule is a deny-list and not an allow-list.
        ("steamuser/Documents/Bioshock/save1.bsg", True),
        ("steamuser/Saved Games/Foo/x.sav", True),
        ("steamuser/AppData/Roaming/Brotato/save.json", True),
        ("steamuser/AppData/LocalLow/ustwo games/x.dat", True),
        ("steamuser/AppData/Local/Bioshock/save.dat", True),
        ("Public/Documents/x.sav", True),
        # A tree handed in already rooted at the user dir still filters.
        ("AppData/Local/Programs/x/y.exe", False),
        ("AppData/Roaming/Brotato/save.json", True),
    ],
)
def test_is_migratable_classifies_paths(relpath, migratable):
    assert sm._is_migratable(Path(relpath)) is migratable


def test_merge_users_skips_program_installations(tmp_path):
    """The 939 MB case: a vendor launcher must not follow saves forward."""
    src = tmp_path / "src"
    programs = src / "steamuser" / "AppData" / "Local" / "Programs" / "CDPR"
    programs.mkdir(parents=True)
    (programs / "REDlauncher.exe").write_text("x" * 4096)
    saves = src / "steamuser" / "Documents" / "Bioshock"
    saves.mkdir(parents=True)
    (saves / "save1.bsg").write_text("savegame")

    copied = sm._merge_users(src, tmp_path / "dst")

    assert copied == 1
    dst = tmp_path / "dst" / "steamuser"
    assert (dst / "Documents" / "Bioshock" / "save1.bsg").read_text() == "savegame"
    assert not (dst / "AppData" / "Local" / "Programs").exists()


def test_merge_users_skips_oversized_file(tmp_path):
    """Belt-and-braces behind the deny-list: no huge payload sneaks through."""
    src = tmp_path / "src"
    game = src / "steamuser" / "AppData" / "Local" / "SomeGame"
    game.mkdir(parents=True)
    (game / "huge.pak").write_bytes(b"\0" * 128)
    (game / "small.sav").write_text("save")

    # Cap below the "huge" file so the guard fires without writing 64 MiB.
    with patch.object(sm, "_MAX_SAVE_FILE_BYTES", 64):
        copied = sm._merge_users(src, tmp_path / "dst")

    assert copied == 1
    dst = tmp_path / "dst" / "steamuser" / "AppData" / "Local" / "SomeGame"
    assert (dst / "small.sav").exists()
    assert not (dst / "huge.pak").exists()


