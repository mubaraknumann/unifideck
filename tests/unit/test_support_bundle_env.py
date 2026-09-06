"""Tests for the device, OS and storage probes.

Two things these must get right: they have to work on machines that
look nothing like a Steam Deck (CI containers have no DMI directory and
may have no ``lsblk``), and they must never ship a secret from the
environment.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.services.support_bundle import (
    env_report,
    inventory,
    probe_conflicts,
    probe_device,
    probe_stack,
    probe_storage,
)
from unifideck.services.support_bundle.spec import BundleContext

SENTINEL = "SUPERSECRET-DO-NOT-SHIP"

_STEAMOS_RELEASE = """\
NAME="SteamOS"
ID=steamos
ID_LIKE=arch
VERSION_ID=3.6.20
BUILD_ID=20240701.1
VARIANT_ID=steamdeck
PRETTY_NAME="SteamOS 3.6.20"
"""

_BAZZITE_RELEASE = """\
NAME="Bazzite"
ID=bazzite
ID_LIKE="fedora"
VERSION_ID=40
VARIANT_ID=deck
PRETTY_NAME="Bazzite 40 (deck)"
"""

_LSBLK = {
    "blockdevices": [
        {
            "name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk",
            "size": 512110190592, "fstype": None, "rm": False,
            "tran": "nvme", "model": "INTERNAL SSD", "mountpoints": [None],
            "children": [
                {
                    "name": "nvme0n1p8", "path": "/dev/nvme0n1p8",
                    "type": "part", "size": 491000000000, "fstype": "ext4",
                    "label": "home", "rm": False,
                    "mountpoints": ["/home", "/var/tmp"],
                },
            ],
        },
        {
            "name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk",
            "size": 400000000000, "rm": True, "mountpoints": [None],
            "children": [
                {
                    "name": "mmcblk0p1", "path": "/dev/mmcblk0p1",
                    "type": "part", "size": 399000000000, "fstype": "ext4",
                    "label": "SteamDeckSD", "rm": True,
                    "mountpoints": ["/run/media/deck/SteamDeckSD"],
                },
            ],
        },
        {
            "name": "sda", "path": "/dev/sda", "type": "disk",
            "size": 1000000000000, "rm": True, "tran": "usb",
            "hotplug": True, "model": "Portable", "mountpoints": [None],
            "children": [
                {
                    "name": "sda1", "path": "/dev/sda1", "type": "part",
                    "size": 999000000000, "fstype": "exfat", "rm": True,
                    "hotplug": True, "tran": "usb", "mountpoints": [None],
                },
            ],
        },
    ],
}


class _FakeConfig:
    def get_str(self, key: str, default: str = "") -> str:
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        return default

    def get(self, key: str, default: Any = None) -> Any:
        return default


def _ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BundleContext:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return BundleContext(
        roots={"data": str(home / "data"), "steam": None, "plugin": None},
        root_sources={"data": "config"},
        config=_FakeConfig(),
        paths=None,
    )


# ── report assembly ───────────────────────────────────────────────
def test_report_is_json_serialisable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No Path, dataclass or MountInfo may leak into the JSON."""
    report = env_report.build_environment_report(_ctx(monkeypatch, tmp_path))
    json.dumps(report)


def test_one_failing_probe_does_not_sink_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A machine that breaks one probe still gets every other block."""
    def _explode() -> dict[str, Any]:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(probe_device, "gpu_block", _explode)
    report = env_report.build_environment_report(_ctx(monkeypatch, tmp_path))
    assert "error" in report["gpu"]
    assert report["os"]["id"]
    assert "identity" in report


def test_render_text_covers_every_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Nothing may be silently dropped from the human-readable copy."""
    report = env_report.build_environment_report(_ctx(monkeypatch, tmp_path))
    rendered = env_report.render_text(report)
    for key in report:
        assert key in rendered


# ── OS identity ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("content", "expected_id", "expected_variant"),
    [
        (_STEAMOS_RELEASE, "steamos", "steamdeck"),
        (_BAZZITE_RELEASE, "bazzite", "deck"),
    ],
)
def test_os_release_is_parsed_per_distro(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_id: str,
    expected_variant: str,
) -> None:
    """The SteamOS / Bazzite / CachyOS discriminator."""
    def _fake_read(path: Path, limit: int = 65536) -> str:
        return content if path.name == "os-release" else ""

    monkeypatch.setattr(probe_device, "_read_text", _fake_read)
    block = probe_device.os_block()
    assert block["id"] == expected_id
    assert block["variant_id"] == expected_variant
    assert block["pretty_name"]


def test_missing_dmi_degrades_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """CI containers and VMs have no DMI directory."""
    monkeypatch.delenv("UNIFIDECK_DEVICE_TYPE", raising=False)
    monkeypatch.setattr(probe_device, "DMI_PATH", tmp_path / "absent")
    block = probe_device.device_block()
    assert block["available"] is False
    assert "note" in block


def test_session_mode_follows_gamescope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gaming Mode versus Desktop, which many bugs depend on."""
    monkeypatch.setattr(probe_device.procscan, "is_running", lambda _n: True)
    assert probe_device.session_block()["mode"] == "gaming"
    monkeypatch.setattr(probe_device.procscan, "is_running", lambda _n: False)
    assert probe_device.session_block()["mode"] == "desktop"


def test_clock_and_ca_state_is_reported() -> None:
    block = probe_device.time_block()
    assert block["clock_plausible"] is True
    assert "ca_store" in block


# ── environment safety ────────────────────────────────────────────
def test_secret_env_vars_never_reach_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof the block reads an allowlist, not ``dict(os.environ)``.

    A developer machine holds GITHUB_TOKEN and cloud credentials; a
    bundle headed for a public channel must not carry them.
    """
    monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SENTINEL)
    monkeypatch.setenv("GOG_TOKEN", SENTINEL)
    rendered = json.dumps(probe_stack.identity_block())
    rendered += json.dumps(probe_stack.decky_block())
    assert SENTINEL not in rendered


def test_proxy_variables_are_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy breaks store sync, but its URL can embed credentials."""
    monkeypatch.setenv("https_proxy", f"http://user:{SENTINEL}@proxy:8080")
    block = probe_stack.identity_block()
    assert block["proxy_vars_set"]["https_proxy"] is True
    assert SENTINEL not in json.dumps(block)


def test_session_env_records_presence_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe_conflicts.procscan, "iter_processes", list)
    block = probe_conflicts.session_env_block()
    assert block["steam_running"] is False
    assert block["variables"] == {}


def test_prefix_locks_handle_the_nested_ubisoft_layout(tmp_path: Path) -> None:
    """Regression: Ubisoft prefixes sit one level deeper.

    Every other store uses ``prefixes/<id>``; Ubisoft namespaces its own
    under ``prefixes/ubisoft/{.template, .upc-auth, <game-uuid>}``.
    Scanning only the top level found no ``pfx`` there and reported
    nothing, so the store most prone to installs hanging at first-run
    setup contributed no lock state at all.
    """
    prefixes = tmp_path / "data" / "prefixes"
    # Flat layout, as used by Epic / GOG / Amazon.
    flat = prefixes / "1207659257" / "pfx"
    flat.mkdir(parents=True)
    (flat / ".update-timestamp").write_text("1", encoding="utf-8")
    # Nested Ubisoft namespace.
    for name in (".template", ".upc-auth", "41b67b23-c7e1-417b"):
        nested = prefixes / "ubisoft" / name / "pfx"
        nested.mkdir(parents=True)
        (nested / ".update-timestamp").write_text("1", encoding="utf-8")
    (prefixes / "ubisoft" / "41b67b23-c7e1-417b" / "pfx.lock").write_text(
        "", encoding="utf-8",
    )
    (
        prefixes / "ubisoft" / "41b67b23-c7e1-417b"
        / "unifideck_ubisoft_bootstrap.marker"
    ).write_text("", encoding="utf-8")

    found = probe_conflicts.wine_locks_block(str(tmp_path / "data"))
    labels = {item["prefix"] for item in found["prefix_locks"]}
    assert "1207659257" in labels
    assert "ubisoft/.template" in labels
    assert "ubisoft/.upc-auth" in labels
    assert "ubisoft/41b67b23-c7e1-417b" in labels
    # The namespace directory itself is not a prefix and must not appear.
    assert "ubisoft" not in labels

    game = next(
        item for item in found["prefix_locks"]
        if item["prefix"] == "ubisoft/41b67b23-c7e1-417b"
    )
    assert game["update_timestamp"]["present"] is True
    assert game["pfx_lock_present"] is True
    assert "unifideck_ubisoft_bootstrap.marker" in game["markers"]


def test_inventory_reports_excluded_areas_without_reading_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Not collected must not mean invisible.

    A prefix, a browser profile and a save backup are all excluded from
    the archive on purpose, but "we did not ship it" and "it is not on
    the device" are different answers, so their existence is reported.
    """
    home = tmp_path / "home"
    data = home / ".local" / "share" / "unifideck"
    (data / "prefixes" / "ubisoft" / "abc123" / "pfx").mkdir(parents=True)
    (data / "edge-auth").mkdir(parents=True)
    (data / "edge-auth" / "Cookies").write_text(SENTINEL, encoding="utf-8")
    (data / "save_backups" / "Some Game").mkdir(parents=True)
    (data / "save_backups" / "Some Game" / "save.dat").write_text(
        SENTINEL, encoding="utf-8",
    )
    games = home / "Games"
    (games / "Installed Game").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    ctx = BundleContext(
        roots={"data": str(data), "home": str(home), "config": "", "launches": ""},
        root_sources={},
    )
    text = inventory.build_inventory(ctx, [str(games)])

    # Existence is reported...
    assert "prefixes" in text
    assert "ubisoft" in text
    assert "Cookies" in text
    assert "Some Game" in text
    assert "Installed Game" in text
    # ...but not one byte of content.
    assert SENTINEL not in text


def test_inventory_probes_ubisoft_upc_state_by_name(tmp_path: Path) -> None:
    """Absence of UPC state is itself the diagnostic.

    A prefix holding upc.exe but no ownership file has Ubisoft Connect
    installed and no entitlement data - a specific, nameable failure
    that a directory walk would bury.
    """
    data = tmp_path / "data"
    prefix = data / "prefixes" / "ubisoft" / "game-uuid"
    upc = prefix / "pfx/drive_c/Program Files (x86)/Ubisoft/Ubisoft Game Launcher"
    upc.mkdir(parents=True)
    (upc / "upc.exe").write_text("MZ", encoding="utf-8")
    ctx = BundleContext(roots={"data": str(data), "home": str(tmp_path)})
    text = inventory.build_inventory(ctx, [])
    assert "ubisoft_upc_state" in text
    assert "EXISTS   pfx/drive_c/Program Files (x86)/Ubisoft" in text
    assert "absent   pfx/drive_c/ProgramData/Ubisoft/Ubisoft Game Launcher/ownership" in text


def test_inventory_is_bounded_on_a_huge_tree(tmp_path: Path) -> None:
    """A Wine prefix holds tens of thousands of files."""
    data = tmp_path / "data"
    flood = data / "prefixes" / "big"
    flood.mkdir(parents=True)
    for index in range(inventory._ENTRY_CAP + 120):
        (flood / f"file{index:05d}").write_text("x", encoding="utf-8")
    ctx = BundleContext(roots={"data": str(data), "home": str(tmp_path)})
    text = inventory.build_inventory(ctx, [])
    assert "entry cap reached" in text
    assert len(text.splitlines()) < inventory._ENTRY_CAP * 3


def test_prefix_probe_reads_no_prefix_contents(tmp_path: Path) -> None:
    """Prefixes hold the user's Wine registry; we only stat markers."""
    prefix = tmp_path / "data" / "prefixes" / "game" / "pfx"
    prefix.mkdir(parents=True)
    (prefix / ".update-timestamp").write_text("1", encoding="utf-8")
    secret = prefix / "drive_c" / "users" / "steamuser"
    secret.mkdir(parents=True)
    (secret / "user.reg").write_text(SENTINEL, encoding="utf-8")
    found = probe_conflicts.wine_locks_block(str(tmp_path / "data"))
    assert SENTINEL not in json.dumps(found)


# ── storage ───────────────────────────────────────────────────────
def _with_lsblk(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    monkeypatch.setattr(probe_storage, "_run_lsblk", lambda: payload)
    monkeypatch.setattr(probe_storage, "_plugin_view", lambda: ({}, "ok"))


def test_devices_are_classified_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_lsblk(monkeypatch, _LSBLK["blockdevices"])
    devices = probe_storage.storage_block(_FakeConfig())["devices"]
    by_name = {item["name"]: item for item in devices}
    assert by_name["nvme0n1p8"]["class"] == "internal"
    assert by_name["mmcblk0p1"]["class"] == "sdcard"
    assert by_name["sda1"]["class"] == "usb"


def test_unmounted_partition_keeps_its_filesystem_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason lsblk is the primary probe.

    An unmounted exFAT partition is the "my drive is not detected" case,
    and neither /proc/mounts nor unprivileged blkid can name its
    filesystem.
    """
    _with_lsblk(monkeypatch, _LSBLK["blockdevices"])
    devices = probe_storage.storage_block(_FakeConfig())["devices"]
    usb = next(item for item in devices if item["name"] == "sda1")
    assert usb["mounted_at"] == ""
    assert usb["fstype"] == "exfat"
    assert "not mounted" in usb["visibility_note"]


def test_multiple_mountpoints_count_as_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: only the first mount point used to be considered.

    SteamOS mounts the home partition several times, so checking one
    made a healthy internal disk look invisible to the plugin.
    """
    monkeypatch.setattr(probe_storage, "_run_lsblk", lambda: _LSBLK["blockdevices"])
    monkeypatch.setattr(
        probe_storage,
        "_plugin_view",
        lambda: ({"/home": {"fstype": "ext4", "in_picker": True}}, "ok"),
    )
    devices = probe_storage.storage_block(_FakeConfig())["devices"]
    home = next(item for item in devices if item["name"] == "nvme0n1p8")
    assert home["visible_to_plugin"] is True
    assert home["all_mountpoints"] == ["/home", "/var/tmp"]


def test_visible_but_unwritable_mount_is_not_reported_as_undetected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different failures must not share one verdict.

    A mount the scanner sees but the picker refuses for writability is
    an ownership problem on the mount root, not a detection problem —
    reporting it as "not detected" sent the last triage round chasing
    the scanner instead.
    """
    monkeypatch.setattr(probe_storage, "_run_lsblk", lambda: _LSBLK["blockdevices"])
    monkeypatch.setattr(
        probe_storage,
        "_plugin_view",
        lambda: (
            {
                "/run/media/deck/SteamDeckSD": {
                    "fstype": "ext4", "writable": False, "in_picker": False,
                },
            },
            "ok",
        ),
    )
    block = probe_storage.storage_block(_FakeConfig())
    sd = next(item for item in block["devices"] if item["name"] == "mmcblk0p1")
    assert sd["visible_to_plugin"] is True
    assert sd["offered_in_picker"] is False
    assert "NOT writable" in sd["visibility_note"]
    assert "not reported by the plugin" not in sd["visibility_note"]
    assert block["plugin_view_mount_points"] == ["/run/media/deck/SteamDeckSD"]
    assert block["plugin_view_picker_mount_points"] == []


def test_plugin_view_records_both_scanner_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``in_picker`` comes from the strict scan the picker really calls."""
    lenient = tmp_path / "lenient"
    strict = tmp_path / "strict"
    for path in (lenient, strict):
        path.mkdir()

    def fake_scan(_home_dev: int, *, require_writable: bool = True) -> list[Any]:
        chosen = strict if require_writable else lenient
        return [
            SimpleNamespace(
                mount_point=str(chosen), device="/dev/sda1", fstype="ext4",
                writable=require_writable, effective_uid=None, options={},
            ),
        ]

    monkeypatch.setattr("unifideck.utils.mounts.scan_mounts", fake_scan)
    view, status = probe_storage._plugin_view()
    assert status == "ok"
    assert view[str(lenient)]["in_picker"] is False
    assert list(view) == [str(lenient)]


def test_fstype_lookup_handles_a_spaced_mount_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Shared decoder, so this can't drift from the real scanner again."""
    mounts_file = tmp_path / "proc-mounts"
    mounts_file.write_text(
        "/dev/sda1 /run/media/deck/External\\040SSD exfat rw 0 0\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        probe_storage, "_read_str", lambda _p: mounts_file.read_text(encoding="utf-8"),
    )
    assert probe_storage._fstype_for("/run/media/deck/External SSD/Games") == "exfat"


def test_internal_disk_is_not_treated_as_user_storage() -> None:
    internal = {"class": "internal", "all_mountpoints": ["/var/tmp"]}
    sdcard = {"class": "sdcard", "all_mountpoints": ["/run/media/deck/SD"]}
    assert probe_storage.is_user_storage(internal) is False
    assert probe_storage.is_user_storage(sdcard) is True


def test_sys_block_fallback_admits_unknown_filesystems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without lsblk we say "unknown" rather than guessing."""
    monkeypatch.setattr(probe_storage, "_run_lsblk", lambda: None)
    monkeypatch.setattr(probe_storage, "_plugin_view", lambda: ({}, "ok"))
    block = probe_storage.storage_block(_FakeConfig())
    assert block["source"] == "sys_block_fallback"
    for device in block["devices"]:
        assert device["fstype"] == "unknown"


def test_lsblk_is_never_asked_for_serial_numbers() -> None:
    """Serials identify hardware and diagnose nothing."""
    assert "SERIAL" not in probe_storage._LSBLK_COLUMNS
    assert "WWN" not in probe_storage._LSBLK_COLUMNS


def test_storage_probe_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """It must not create the ``games`` subdirectory it looks for."""
    home = tmp_path / "home"
    (home / "Games").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _with_lsblk(monkeypatch, _LSBLK["blockdevices"])
    before = {str(path) for path in home.rglob("*")}
    probe_storage.storage_block(_FakeConfig())
    assert {str(path) for path in home.rglob("*")} == before


def test_fuse_conf_state_is_reported() -> None:
    """The literal switch behind FUSE-invisible drives."""
    block = probe_storage._fuse_conf()
    assert "user_allow_other" in block
    assert isinstance(block["present"], bool)
