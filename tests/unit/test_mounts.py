"""Tests for utils/mounts.py — shared external-storage mount enumeration.

Covers the FUSE-mounted-external-storage regression: a mount owned
by a uid other than this process's (the ``uid=``/``gid=`` options
ntfs-3g/fuse-exfat mounts carry) must be reachable via a demoted
subprocess rather than silently excluded, and — separately — a
permission-denied mount with no such option must be excluded
cleanly rather than raising (the confirmed uncaught-``PermissionError``
bug in the pre-fix per-file scanners).
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from unifideck.utils import mounts


def _write_mounts(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "proc-mounts"
    p.write_text("\n".join(lines) + "\n")
    return p


def _kernel_field(path: Path | str) -> str:
    """Render *path* the way the kernel writes it into ``/proc/mounts``.

    Backslash first, then space — the same order the kernel escapes in,
    so a path holding both round-trips.
    """
    return str(path).replace("\\", r"\134").replace(" ", r"\040")


# ─── parse_mount_options / is_eligible_type / stat_dev ─────────


def test_parse_mount_options_extracts_uid_gid() -> None:
    assert mounts.parse_mount_options("rw,nosuid,uid=1000,gid=1000,umask=0022") == {
        "rw": "",
        "nosuid": "",
        "uid": "1000",
        "gid": "1000",
        "umask": "0022",
    }


def test_parse_mount_options_no_uid_option() -> None:
    assert mounts.parse_mount_options("rw,relatime") == {"rw": "", "relatime": ""}


def test_parse_mount_options_empty_string_never_raises() -> None:
    assert mounts.parse_mount_options("") == {}


@pytest.mark.parametrize("fstype", ["exfat", "ntfs3", "fuseblk", "vfat", "btrfs", "xfs", "ext4"])
def test_is_eligible_type_allows_real_filesystems(fstype: str) -> None:
    assert mounts.is_eligible_type(fstype, "/run/media/deck/CARD") is True


@pytest.mark.parametrize("fstype", ["tmpfs", "proc", "sysfs", "autofs", "overlay"])
def test_is_eligible_type_skips_virtual_fstypes(fstype: str) -> None:
    assert mounts.is_eligible_type(fstype, "/run/media/deck/CARD") is False


@pytest.mark.parametrize("prefix", ["/dev/", "/sys/", "/proc/", "/run/user/1000/"])
def test_is_eligible_type_skips_virtual_prefixes(prefix: str) -> None:
    assert mounts.is_eligible_type("ext4", prefix + "x") is False


def test_stat_dev_returns_zero_on_missing_path(tmp_path: Path) -> None:
    assert mounts.stat_dev(str(tmp_path / "does-not-exist")) == 0


def test_stat_dev_real_path(tmp_path: Path) -> None:
    assert mounts.stat_dev(str(tmp_path)) == tmp_path.stat().st_dev


# ─── mount_id / is_sdcard_source ────────────────────────────────


def test_mount_id_unique_for_two_simultaneous_external_mounts() -> None:
    assert mounts.legacy_mount_id("/run/media/deck/SDCARD") == "ext:SDCARD"
    assert mounts.legacy_mount_id("/run/media/deck/USBDRIVE") == "ext:USBDRIVE"
    assert mounts.legacy_mount_id("/run/media/deck/SDCARD") != mounts.legacy_mount_id(
        "/run/media/deck/USBDRIVE",
    )


def test_is_sdcard_source_detects_mmcblk() -> None:
    assert mounts.is_sdcard_source("/dev/mmcblk0p1") is True
    assert mounts.is_sdcard_source("/dev/sda1") is False


# ─── dedupe_by_device ───────────────────────────────────────────


def _mount_info(mount_point: str, st_dev: int, **overrides: object) -> mounts.MountInfo:
    base: dict[str, object] = {
        "device": "/dev/sda1", "mount_point": mount_point, "fstype": "ext4",
        "st_dev": st_dev, "options": {}, "writable": True,
    }
    base.update(overrides)
    return mounts.MountInfo(**base)  # type: ignore[arg-type]


def test_dedupe_by_device_collapses_same_st_dev() -> None:
    a = _mount_info("/mnt/a", 42)
    b = _mount_info("/mnt/b", 42)
    assert mounts.dedupe_by_device([a, b]) == [a]


def test_dedupe_by_device_keeps_distinct_zero_entries() -> None:
    a = _mount_info("/mnt/a", 0)
    b = _mount_info("/mnt/b", 0)
    assert mounts.dedupe_by_device([a, b]) == [a, b]


# ─── run_demoted ────────────────────────────────────────────────


def test_run_demoted_happy_path_real_subprocess() -> None:
    proc = mounts.run_demoted(["true"], os.geteuid())
    assert proc is not None
    assert proc.returncode == 0


def test_run_demoted_missing_binary_returns_none() -> None:
    assert mounts.run_demoted(["/nonexistent/binary-xyz"], os.geteuid()) is None


def test_run_demoted_swallows_any_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_perm(*args: object, **kwargs: object) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(subprocess, "run", raise_perm)
    assert mounts.run_demoted(["true"], 1000) is None


# ─── ensure_games_subdir ────────────────────────────────────────


def test_ensure_games_subdir_direct_when_no_effective_uid(tmp_path: Path) -> None:
    result = mounts.ensure_games_subdir(str(tmp_path), None)
    assert result == str(tmp_path / "Games")
    assert (tmp_path / "Games").is_dir()


def test_ensure_games_subdir_demoted_when_effective_uid_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = mounts.ensure_games_subdir(str(tmp_path), 1000, 1000)
    assert result == str(tmp_path / "Games")
    assert not (tmp_path / "Games").is_dir(), "must not mkdir directly once demoted"
    assert len(calls) == 1
    assert calls[0][1]["user"] == 1000
    assert calls[0][1]["group"] == 1000


def test_ensure_games_subdir_returns_mount_point_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=""),
    )
    assert mounts.ensure_games_subdir(str(tmp_path), 1000) == str(tmp_path)


# ─── mount_is_dir / mount_child_dirs ────────────────────────────


def test_mount_is_dir_direct(tmp_path: Path) -> None:
    assert mounts.mount_is_dir(str(tmp_path), None) is True
    assert mounts.mount_is_dir(str(tmp_path / "nope"), None) is False


def test_mount_is_dir_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    assert mounts.mount_is_dir("/some/fuse/mount", 1000) is True


def test_mount_child_dirs_direct(tmp_path: Path) -> None:
    (tmp_path / "Games").mkdir()
    (tmp_path / "GOG Games").mkdir()
    (tmp_path / "afile.txt").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "Games")
    children = {p.name for p in mounts.mount_child_dirs(str(tmp_path), None)}
    assert children == {"Games", "GOG Games"}


def test_mount_child_dirs_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="/mnt/card/Games\n/mnt/card/GOG Games\n", stderr="",
        ),
    )
    children = mounts.mount_child_dirs("/mnt/card", 1000)
    assert children == [Path("/mnt/card/Games"), Path("/mnt/card/GOG Games")]


# ─── scan_mounts: the end-to-end regression tests ───────────────


def test_scan_mounts_direct_access_no_uid_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-FUSE mount, no uid= option: today's fast path, no demotion."""
    ext = tmp_path / "ext-drive"
    ext.mkdir()
    mounts_file = _write_mounts(tmp_path, [
        f"/dev/sda1 {ext} ext4 rw,relatime 0 0",
    ])

    called = False

    def fail_if_called(*a: object, **k: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("subprocess.run must not be called for a directly-accessible mount")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    # A sentinel home_dev distinct from tmp_path's real st_dev — tmp_path
    # and any subdir of it share one filesystem, so a real "home" subdir
    # would collide with ext-drive's st_dev and get excluded for the
    # wrong reason.
    result = mounts.scan_mounts(999999, mounts_path=mounts_file, require_writable=True)
    assert not called
    assert len(result) == 1
    assert result[0].mount_point == str(ext)
    assert result[0].effective_uid is None
    assert result[0].writable is True


def test_scan_mounts_excludes_home_device(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    mounts_file = _write_mounts(tmp_path, [
        f"/dev/nvme0n1p8 {home} ext4 rw,relatime 0 0",
    ])
    result = mounts.scan_mounts(mounts.stat_dev(str(home)), mounts_path=mounts_file)
    assert result == []


def test_scan_mounts_permission_denied_no_uid_option_excludes_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: must not raise PermissionError out of scan_mounts."""
    mounts_file = _write_mounts(tmp_path, [
        "/dev/sdb1 /mnt/weird ext4 rw,relatime 0 0",
    ])

    def raise_denied(self: Path) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_dir", raise_denied)

    result = mounts.scan_mounts(999999, mounts_path=mounts_file)
    assert result == []


def test_scan_mounts_fuse_uid_mismatch_demotes_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounts_file = _write_mounts(tmp_path, [
        "/dev/sdb1 /run/media/deck/NTFSCARD fuseblk rw,uid=1000,gid=1000 0 0",
    ])

    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def deny_direct(self: Path) -> bool:
        return False

    monkeypatch.setattr(Path, "is_dir", deny_direct)

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[:2] == ["test", "-d"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["test", "-w"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["stat", "-c"]:
            return subprocess.CompletedProcess(argv, 0, stdout="99\n", stderr="")
        raise AssertionError(f"unexpected demoted call: {argv}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = mounts.scan_mounts(1, mounts_path=mounts_file, require_writable=True)
    assert len(result) == 1
    info = result[0]
    assert info.effective_uid == 1000
    assert info.effective_gid == 1000
    assert info.writable is True
    assert info.st_dev == 99
    assert all(c[0][0] in ("test", "stat") for c in calls)
    assert all(c[1].get("user") == 1000 and c[1].get("group") == 1000 for c in calls)


def test_scan_mounts_fuse_uid_mismatch_demotion_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounts_file = _write_mounts(tmp_path, [
        "/dev/sdb1 /run/media/deck/NTFSCARD fuseblk rw,uid=1000,gid=1000 0 0",
    ])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=""),
    )

    result = mounts.scan_mounts(1, mounts_path=mounts_file)
    assert result == []


def test_scan_mounts_require_writable_false_accepts_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ext = tmp_path / "ro-drive"
    ext.mkdir()
    mounts_file = _write_mounts(tmp_path, [
        f"/dev/sda1 {ext} ext4 ro,relatime 0 0",
    ])
    monkeypatch.setattr(os, "access", lambda *a, **k: False)

    result = mounts.scan_mounts(999999, mounts_path=mounts_file, require_writable=False)
    assert len(result) == 1
    assert result[0].writable is False

    result_strict = mounts.scan_mounts(999999, mounts_path=mounts_file, require_writable=True)
    assert result_strict == []


def test_scan_mounts_two_simultaneous_external_mounts_get_distinct_ids(
    tmp_path: Path,
) -> None:
    sd = tmp_path / "SDCARD"
    sd.mkdir()
    usb = tmp_path / "USBDRIVE"
    usb.mkdir()
    mounts_file = _write_mounts(tmp_path, [
        f"/dev/mmcblk0p1 {sd} ext4 rw,relatime 0 0",
        f"/dev/sda1 {usb} ext4 rw,relatime 0 0",
    ])

    result = mounts.scan_mounts(999999, mounts_path=mounts_file)
    ids = {mounts.mount_id(m) for m in result}
    assert len(result) == 2
    assert len(ids) == 2


def test_scan_mounts_missing_file_returns_empty(tmp_path: Path) -> None:
    assert mounts.scan_mounts(0, mounts_path=tmp_path / "nope") == []


def test_scan_mounts_finds_mount_point_containing_a_space(tmp_path: Path) -> None:
    """The "my external drive isn't detected" regression.

    udisks2 mounts a drive at ``/run/media/<user>/<label>``, so a label
    with a space reaches ``/proc/mounts`` octal-escaped. Undecoded it
    named no existing path, failed its ``is_dir()`` probe, and vanished
    from the install picker — while lsblk, Steam and the desktop all
    saw the drive perfectly.
    """
    ext = tmp_path / "External SSD"
    ext.mkdir()
    mounts_file = _write_mounts(tmp_path, [
        f"/dev/sda1 {_kernel_field(ext)} ext4 rw,nosuid 0 0",
    ])

    result = mounts.scan_mounts(999999, mounts_path=mounts_file, require_writable=True)

    assert len(result) == 1
    assert result[0].mount_point == str(ext)
    assert " " in result[0].mount_point
    assert result[0].writable is True


def test_spaced_mount_point_yields_a_path_safe_id(tmp_path: Path) -> None:
    """The id stays space-free so the install-path resolver can match it.

    ``by_uuid_dir`` is passed explicitly at an empty index so the mount has
    no UUID and the name-derived fallback is what gets asserted. Without it
    the default is the real ``/dev/disk/by-uuid`` and the result depends on
    whether the host happens to index a UUID for ``/dev/sda1``: this test
    passed locally and on CI's 3.12 runner while failing on its 3.11 runner,
    which had one, returning ``ext:1f78b26…`` instead.
    """
    ext = tmp_path / "External SSD"
    ext.mkdir()
    empty_index = tmp_path / "by-uuid-empty"
    empty_index.mkdir()
    mounts_file = _write_mounts(tmp_path, [
        f"/dev/sda1 {_kernel_field(ext)} ext4 rw 0 0",
    ])
    result = mounts.scan_mounts(
        999999, mounts_path=mounts_file, by_uuid_dir=empty_index,
    )
    ids = [loc_id for loc_id, _ in mounts.assign_unique_ids(result)]
    assert ids == ["ext:External_SSD"]


def test_scan_mounts_survives_a_non_utf8_mounts_file(tmp_path: Path) -> None:
    """One undecodable label must not take the whole scan down with it.

    ``read_text()`` raises ``UnicodeDecodeError`` — a ``ValueError``, so
    the ``except OSError`` around the read never caught it and it
    propagated out of ``get_storage_locations``, emptying the picker of
    every location including internal storage.
    """
    good = tmp_path / "GOOD"
    good.mkdir()
    mounts_file = tmp_path / "proc-mounts"
    mounts_file.write_bytes(
        b"/dev/sdb1 /run/media/deck/Disque\xa0Dur ext4 rw 0 0\n"
        + f"/dev/sda1 {good} ext4 rw 0 0\n".encode(),
    )

    result = mounts.scan_mounts(999999, mounts_path=mounts_file)

    assert [m.mount_point for m in result] == [str(good)]


def test_an_undecodable_label_is_logged_as_utf8(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The skip warning for that label must be encodable, or the handler drops it."""
    mounts_file = tmp_path / "proc-mounts"
    mounts_file.write_bytes(b"/dev/sdb1 /run/media/deck/Disque\xa0Dur ext4 rw 0 0\n")

    with caplog.at_level(logging.DEBUG, logger=mounts.logger.name):
        mounts.scan_mounts(999999, mounts_path=mounts_file)

    skipped = [r.getMessage() for r in caplog.records if "Disque" in r.getMessage()]
    assert skipped and all("Disque\\xa0Dur" in m for m in skipped)
    for message in skipped:
        message.encode("utf-8")


# ─── identity: UUID-based ids ──────────────────────────────────


def _fake_uuid_index(tmp_path: Path, mapping: dict[str, str]) -> Path:
    """A stand-in ``/dev/disk/by-uuid`` — uuid symlink → device node."""
    root = tmp_path / "by-uuid"
    root.mkdir()
    for uuid, device in mapping.items():
        (root / uuid).symlink_to(device)
    return root


def test_scan_mounts_attaches_the_filesystem_uuid(tmp_path: Path) -> None:
    ext = tmp_path / "External SSD"
    ext.mkdir()
    node = tmp_path / "sda1"
    node.touch()
    root = _fake_uuid_index(tmp_path, {"b430ddca-dece-4f36-b839-ab71e1b4efed": str(node)})
    mounts_file = _write_mounts(tmp_path, [
        f"{node} {_kernel_field(ext)} ext4 rw 0 0",
    ])

    result = mounts.scan_mounts(999999, mounts_path=mounts_file, by_uuid_dir=root)

    assert len(result) == 1
    assert result[0].uuid == "b430ddca-dece-4f36-b839-ab71e1b4efed"
    assert mounts.mount_id(result[0]) == "ext:b430ddca-dece-4f36-b839-ab71e1b4efed"


def test_id_survives_relabelling_the_drive(tmp_path: Path) -> None:
    """The point of UUID ids: identity must not live in the label.

    Under the old name-derived scheme, renaming a drive silently
    orphaned the saved ``download.default_location`` that pointed at it.
    """
    node = tmp_path / "sda1"
    node.touch()
    root = _fake_uuid_index(tmp_path, {"b430ddca-dece-4f36-b839-ab71e1b4efed": str(node)})
    ids = []
    for label in ("External SSD", "Renamed Later"):
        drive = tmp_path / label
        drive.mkdir()
        mounts_file = _write_mounts(tmp_path, [f"{node} {_kernel_field(drive)} ext4 rw 0 0"])
        found = mounts.scan_mounts(999999, mounts_path=mounts_file, by_uuid_dir=root)
        ids.append(mounts.mount_id(found[0]))
    assert ids[0] == ids[1]
    assert len(set(ids)) == 1


def test_mount_id_falls_back_to_the_name_without_a_uuid() -> None:
    """Network shares and some FUSE mounts have no device node to index."""
    m = _mount_info("/run/media/deck/External SSD", 11)
    assert m.uuid == ""
    assert mounts.mount_id(m) == "ext:External_SSD"
    assert mounts.mount_id(m) == mounts.legacy_mount_id(m.mount_point)


def test_cloned_filesystems_sharing_a_uuid_still_get_unique_ids() -> None:
    """A dd-copied drive genuinely repeats its UUID."""
    a = _mount_info("/run/media/deck/ORIGINAL", 11, uuid="same-uuid")
    b = _mount_info("/run/media/deck/CLONE", 22, uuid="same-uuid")
    ids = [loc_id for loc_id, _ in mounts.assign_unique_ids([a, b])]
    assert ids == ["ext:same-uuid", "ext:same-uuid-22"]
    assert len(set(ids)) == 2


def test_refusing_user_media_is_logged_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped drive must leave a trace above debug level.

    The first report of the escaping bug shipped five session logs that
    mentioned the refused drive exactly zero times.
    """
    with caplog.at_level(logging.DEBUG, logger="unifideck.utils.mounts"):
        mounts.scan_mounts(999999, mounts_path=_write_mounts(tmp_path, [
            "/dev/sda1 /run/media/deck/GONE ext4 rw 0 0",
            "/dev/sdb1 /mnt/system-thing ext4 rw 0 0",
        ]))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("/run/media/deck/GONE" in m for m in warnings)
    assert not any("/mnt/system-thing" in m for m in warnings)


def test_assign_unique_ids_keeps_bare_id_without_collision() -> None:
    a = _mount_info("/run/media/deck/SDCARD", st_dev=11)
    b = _mount_info("/run/media/deck/USB", st_dev=22)
    ids = dict(mounts.assign_unique_ids([a, b]))
    assert set(ids) == {"ext:SDCARD", "ext:USB"}


def test_assign_unique_ids_disambiguates_same_basename() -> None:
    """Two distinct devices sharing a mount-point basename get unique ids."""
    a = _mount_info("/run/media/deck/GAMES", st_dev=11)
    b = _mount_info("/media/GAMES", st_dev=22)
    pairs = mounts.assign_unique_ids([a, b])
    ids = [i for i, _ in pairs]
    assert ids == ["ext:GAMES", "ext:GAMES-22"]  # first bare, later suffixed
    assert len(set(ids)) == 2


def test_assign_unique_ids_stable_across_repeat_calls() -> None:
    """Enumerator + resolver must derive the same ids from the same list."""
    a = _mount_info("/run/media/deck/GAMES", st_dev=11)
    b = _mount_info("/media/GAMES", st_dev=22)
    assert mounts.assign_unique_ids([a, b]) == mounts.assign_unique_ids([a, b])
