"""Tests for ``launcher.ea.fingerprint``: one machine fingerprint per game, launch after launch.

Wine reports the Windows install date as the prefix's ``drive_c`` modification
time, an input of older activation fingerprints. A changed fingerprint makes EA
count another PC.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from unifideck.launcher.ea.fingerprint import drive_c, pin_install_date
from unifideck.launcher.ea.records import InstallRecord, read_record, write_record


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _prefix(home: Path) -> Path:
    prefix = home / "prefixes" / "OFB-EAST__48217"
    (prefix / "pfx" / "drive_c").mkdir(parents=True)
    write_record(InstallRecord(
        game_id="OFB-EAST__48217", offer_id="OFB-EAST:48217", content_id="71592",
        install_path=str(home / "game"), exe="x", build_id="/b.zip", version="1",
    ))
    return prefix


def test_the_first_install_date_is_kept_and_restored(home: Path) -> None:
    prefix = _prefix(home)
    folder = drive_c(prefix)
    os.utime(folder, ns=(1_000, 1_794_835_392_000_000_000))

    pin_install_date("OFB-EAST__48217", prefix)
    record = read_record("OFB-EAST__48217")
    assert record is not None and record.drive_c_mtime_ns == "1794835392000000000"

    # Something writes at the top of drive_c, or the prefix is recreated
    os.utime(folder, ns=(1_000, 1_794_900_000_000_000_000))
    pin_install_date("OFB-EAST__48217", prefix)
    assert folder.stat().st_mtime_ns == 1_794_835_392_000_000_000
    # The recorded value never moves
    record = read_record("OFB-EAST__48217")
    assert record is not None and record.drive_c_mtime_ns == "1794835392000000000"


def test_a_game_without_a_record_or_prefix_is_left_alone(home: Path) -> None:
    pin_install_date("OFB-EAST__1", home / "nowhere")
    prefix = _prefix(home)
    (prefix / "pfx" / "drive_c").rmdir()
    pin_install_date("OFB-EAST__48217", prefix)
    record = read_record("OFB-EAST__48217")
    assert record is not None and record.drive_c_mtime_ns == ""
